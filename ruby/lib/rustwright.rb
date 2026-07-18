# frozen_string_literal: true

require 'json'
require 'monitor'
require 'rbconfig'

module Rustwright
  VERSION = '0.1.1'
  UNSET = Object.new.freeze

  class Error < StandardError; end
  class ClosedError < Error; end

  module Normalize
    module_function

    LAUNCH_KEYS = {
      'headless' => 'headless',
      'executablePath' => 'executable_path',
      'executable_path' => 'executable_path',
      'channel' => 'channel',
      'args' => 'args',
      'ignoreAllDefaultArgs' => 'ignore_all_default_args',
      'ignore_all_default_args' => 'ignore_all_default_args',
      'ignoreDefaultArgs' => 'ignore_default_args',
      'ignore_default_args' => 'ignore_default_args',
      'timeout' => 'timeout',
      'userDataDir' => 'user_data_dir',
      'user_data_dir' => 'user_data_dir',
      'env' => 'env',
      'chromiumSandbox' => 'chromium_sandbox',
      'chromium_sandbox' => 'chromium_sandbox',
      'proxy' => 'proxy'
    }.freeze

    SCREENSHOT_KEYS = {
      'path' => 'path',
      'fullPage' => 'fullPage',
      'full_page' => 'fullPage',
      'clip' => 'clip',
      'timeout' => 'timeout',
      'type' => 'type',
      'quality' => 'quality',
      'omitBackground' => 'omitBackground',
      'omit_background' => 'omitBackground'
    }.freeze

    def launch(options)
      normalize_known_hash(options || {}, LAUNCH_KEYS, 'launch options')
    end

    def screenshot(options)
      return nil if options.nil? || options.empty?

      normalize_known_hash(options, SCREENSHOT_KEYS, 'screenshot options')
    end

    def option_hash(options, keywords, allowed, context)
      base = options.nil? ? {} : options
      raise ArgumentError, "#{context} must be a Hash" unless base.is_a?(Hash)

      merged = base.merge(keywords)
      normalize_known_hash(merged, allowed, context)
    end

    def normalize_known_hash(value, mapping, context)
      raise ArgumentError, "#{context} must be a Hash" unless value.is_a?(Hash)

      value.each_with_object({}) do |(key, item), normalized|
        source_key = key.to_s
        target_key = mapping[source_key]
        raise ArgumentError, "unknown #{context} key #{key.inspect}" unless target_key
        raise ArgumentError, "duplicate #{context} key #{target_key.inspect}" if normalized.key?(target_key)

        normalized[target_key] = json_value(item)
      end
    end

    def json_value(value)
      case value
      when Hash
        value.each_with_object({}) { |(key, item), result| result[key.to_s] = json_value(item) }
      when Array
        value.map { |item| json_value(item) }
      when Symbol
        value.to_s
      else
        value
      end
    end
  end
end

require_relative 'rustwright/native'
require_relative 'rustwright/wire'

module Rustwright
  class << self
    def default_library_path
      # Installed platform gems carry one matching native. Source checkouts keep
      # the historical repository-relative target/release fallback.
      bundled_library_path || source_library_path
    end

    def chromium(library_path: nil)
      # An explicit argument or environment override remains an exact pin.
      path = library_path || ENV['RUSTWRIGHT_CAPI_LIB'] || default_library_path
      Chromium.new(native_for(path))
    end

    def bundled_library_path
      platform, extension = bundled_platform
      return nil unless platform

      path = File.join(__dir__, 'rustwright', 'native', platform, "librustwright_capi.#{extension}")
      File.file?(path) ? path : nil
    end

    def inline_html_url(html)
      raise ArgumentError, 'html must be a String' unless html.is_a?(String)

      encoded = html.encode(Encoding::UTF_8).bytes.map do |byte|
        if (byte >= 65 && byte <= 90) || (byte >= 97 && byte <= 122) ||
           (byte >= 48 && byte <= 57) || [45, 46, 95, 126].include?(byte)
          byte.chr
        else
          format('%%%02X', byte)
        end
      end.join
      "data:text/html;charset=utf-8,#{encoded}"
    end

    private

    def source_library_path
      extension = RbConfig::CONFIG['host_os'].match?(/darwin/) ? 'dylib' : 'so'
      File.join('target', 'release', "librustwright_capi.#{extension}")
    end

    def bundled_platform
      cpu = RbConfig::CONFIG['host_cpu']
      os = RbConfig::CONFIG['host_os']

      if os.match?(/darwin/)
        return ['arm64-darwin', 'dylib'] if cpu.match?(/arm64|aarch64/)
        return ['x86_64-darwin', 'dylib'] if cpu.match?(/x86_64|amd64/)
      elsif os.match?(/linux/)
        return ['aarch64-linux', 'so'] if cpu.match?(/arm64|aarch64/)
        return ['x86_64-linux', 'so'] if cpu.match?(/x86_64|amd64/)
      end

      nil
    end

    def native_for(path)
      expanded = File.expand_path(path)
      native_mutex.synchronize do
        native_instances[expanded] ||= Native.new(expanded)
      end
    end

    def native_instances
      @native_instances ||= {}
    end

    def native_mutex
      @native_mutex ||= Mutex.new
    end
  end

  class Chromium
    def initialize(native)
      @native = native
    end

    def executable_path
      out = @native.pointer_slot
      status = @native.call(:rw_chromium_executable_path, out)
      @native.check_status!(status, 'rw_chromium_executable_path')
      @native.copy_owned_string(@native.pointer_address(out), nullable: true)
    end

    def launch(options = nil, **keywords)
      base = options || {}
      raise ArgumentError, 'launch options must be a Hash' unless base.is_a?(Hash)

      merged = base.merge(keywords)
      normalized = Normalize.launch(merged)
      out = @native.pointer_slot
      status = @native.call(:rw_chromium_launch, JSON.generate(normalized), out)
      @native.check_status!(status, 'rw_chromium_launch')
      handle = @native.pointer_address(out)
      raise Error, 'rw_chromium_launch returned an unexpected NULL browser' if handle.zero?

      Browser.new(@native, handle)
    end
  end

  class Browser
    def initialize(native, handle)
      @native = native
      @handle = handle
      @monitor = Monitor.new
      @pages = []
    end

    def new_page
      synchronize do
        ensure_open!
        out = @native.pointer_slot
        status = @native.call(:rw_browser_new_page, @handle, out)
        @native.check_status!(status, 'rw_browser_new_page')
        page_handle = @native.pointer_address(out)
        raise Error, 'rw_browser_new_page returned an unexpected NULL page' if page_handle.zero?

        Page.new(@native, self, page_handle).tap { |page| @pages << page }
      end
    end

    def ws_endpoint
      synchronize do
        ensure_open!
        pointer = @native.call(:rw_browser_ws_endpoint, @handle)
        @native.raise_null_error!('rw_browser_ws_endpoint') if @native.null?(pointer)
        @native.copy_owned_string(pointer)
      end
    end

    def close
      synchronize do
        return nil if @handle.nil?

        errors = []
        @pages.dup.each do |page|
          begin
            page.close
          rescue StandardError => e
            errors << e
          end
        end

        handle = @handle
        begin
          status = @native.call(:rw_browser_close, handle)
          @native.check_status!(status, 'rw_browser_close')
        rescue StandardError => e
          errors << e
        ensure
          @native.call(:rw_browser_free, handle)
          @handle = nil
          @pages.clear
        end

        raise errors.first unless errors.empty?

        nil
      end
    end

    def closed?
      synchronize { @handle.nil? }
    end

    # Internal synchronization shared by every page belonging to this browser.
    def synchronize(&block)
      @monitor.synchronize(&block)
    end

    def remove_page(page)
      synchronize { @pages.delete(page) }
    end

    private

    def ensure_open!
      raise ClosedError, 'browser is closed' if @handle.nil?
    end
  end

  class Page
    SIMPLE_TIMEOUT_KEYS = { 'timeout' => 'timeout' }.freeze
    GOTO_KEYS = {
      'waitUntil' => 'wait_until',
      'wait_until' => 'wait_until',
      'timeout' => 'timeout',
      'referer' => 'referer'
    }.freeze
    CLOSE_KEYS = {
      'timeout' => 'timeout',
      'runBeforeUnload' => 'run_before_unload',
      'run_before_unload' => 'run_before_unload'
    }.freeze

    def initialize(native, browser, handle)
      @native = native
      @browser = browser
      @handle = handle
      @monitor = Monitor.new
    end

    def target_id
      native_call do
        pointer = @native.call(:rw_page_target_id, @handle)
        @native.raise_null_error!('rw_page_target_id') if @native.null?(pointer)
        @native.copy_owned_string(pointer)
      end
    end

    def goto(url, options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, GOTO_KEYS, 'goto options')
      url = input_string(url, 'url')
      wait_until = nullable_input_string(opts['wait_until'], 'wait_until')
      referer = nullable_input_string(opts['referer'], 'referer')

      native_call do
        out = @native.pointer_slot
        status = @native.call(
          :rw_page_goto,
          @handle,
          url,
          wait_until || 0,
          timeout(opts['timeout']),
          referer || 0,
          out
        )
        @native.check_status!(status, 'rw_page_goto')
        json = @native.copy_owned_string(@native.pointer_address(out), nullable: true)
        json.nil? ? nil : JSON.parse(json)
      end
    end

    def click(selector, options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, SIMPLE_TIMEOUT_KEYS, 'click options')
      selector = input_string(selector, 'selector')
      native_call do
        status = @native.call(:rw_page_click, @handle, selector, timeout(opts['timeout']))
        @native.check_status!(status, 'rw_page_click')
        nil
      end
    end

    def fill(selector, value, options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, SIMPLE_TIMEOUT_KEYS, 'fill options')
      selector = input_string(selector, 'selector')
      value = input_string(value, 'value')
      native_call do
        status = @native.call(:rw_page_fill, @handle, selector, value, timeout(opts['timeout']))
        @native.check_status!(status, 'rw_page_fill')
        nil
      end
    end

    def title(options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, SIMPLE_TIMEOUT_KEYS, 'title options')
      native_call do
        out = @native.pointer_slot
        status = @native.call(:rw_page_title, @handle, timeout(opts['timeout']), out)
        @native.check_status!(status, 'rw_page_title')
        @native.copy_owned_string(@native.pointer_address(out))
      end
    end

    def text_content(selector, options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, SIMPLE_TIMEOUT_KEYS, 'text_content options')
      selector = input_string(selector, 'selector')
      native_call do
        out = @native.pointer_slot
        status = @native.call(:rw_page_text_content, @handle, selector, timeout(opts['timeout']), out)
        @native.check_status!(status, 'rw_page_text_content')
        @native.copy_owned_string(@native.pointer_address(out), nullable: true)
      end
    end

    def evaluate(expression, argument = UNSET, options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, SIMPLE_TIMEOUT_KEYS, 'evaluate options')
      expression = input_string(expression, 'expression')
      argument_json = argument.equal?(UNSET) ? nil : JSON.generate(argument)
      native_call do
        out = @native.pointer_slot
        status = @native.call(
          :rw_page_evaluate,
          @handle,
          expression,
          argument_json || 0,
          timeout(opts['timeout']),
          out
        )
        @native.check_status!(status, 'rw_page_evaluate')
        json = @native.copy_owned_string(@native.pointer_address(out))
        Wire.decode(JSON.parse(json))
      end
    end

    def screenshot(options = nil, **keywords)
      base = options || {}
      raise ArgumentError, 'screenshot options must be a Hash' unless base.is_a?(Hash)

      merged = base.merge(keywords)
      normalized = Normalize.screenshot(merged)
      options_json = normalized.nil? ? nil : JSON.generate(normalized)
      native_call do
        out_buffer = @native.pointer_slot
        out_length = @native.size_slot
        status = @native.call(
          :rw_page_screenshot,
          @handle,
          options_json || 0,
          out_buffer,
          out_length
        )
        @native.check_status!(status, 'rw_page_screenshot')
        @native.copy_owned_bytes(
          @native.pointer_address(out_buffer),
          @native.size_value(out_length)
        )
      end
    end

    def close(options = nil, **keywords)
      opts = Normalize.option_hash(options, keywords, CLOSE_KEYS, 'close options')
      @browser.synchronize do
        @monitor.synchronize do
          return nil if @handle.nil?

          handle = @handle
          begin
            status = @native.call(
              :rw_page_close,
              handle,
              timeout(opts['timeout']),
              opts['run_before_unload'] ? 1 : 0
            )
            @native.check_status!(status, 'rw_page_close')
          ensure
            @native.call(:rw_page_free, handle)
            @handle = nil
            @browser.remove_page(self)
          end
        end
      end
      nil
    end

    def closed?
      @browser.synchronize { @monitor.synchronize { @handle.nil? } }
    end

    private

    def native_call
      @browser.synchronize do
        @monitor.synchronize do
          raise ClosedError, 'page is closed' if @handle.nil?
          raise ClosedError, 'browser is closed' if @browser.closed?

          yield
        end
      end
    end

    def timeout(value)
      return Float::NAN if value.nil?

      Float(value)
    rescue ArgumentError, TypeError
      raise ArgumentError, "timeout must be numeric, got #{value.inspect}"
    end

    def input_string(value, name)
      raise ArgumentError, "#{name} must be a String" unless value.is_a?(String)

      value.encode(Encoding::UTF_8)
    end

    def nullable_input_string(value, name)
      value.nil? ? nil : input_string(value, name)
    end
  end
end
