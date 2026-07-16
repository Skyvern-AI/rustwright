# frozen_string_literal: true

require 'fiddle'

module Rustwright
  # Thin, ownership-aware wrapper around the complete rustwright C ABI.
  class Native
    VOIDP = Fiddle::TYPE_VOIDP
    INT = Fiddle::TYPE_INT
    DOUBLE = Fiddle::TYPE_DOUBLE
    SIZE_T = Fiddle::TYPE_SIZE_T
    VOID = Fiddle::TYPE_VOID

    SIGNATURES = {
      rw_last_error: [[], VOIDP],
      rw_string_free: [[VOIDP], VOID],
      rw_bytes_free: [[VOIDP, SIZE_T], VOID],
      rw_chromium_executable_path: [[VOIDP], INT],
      rw_chromium_launch: [[VOIDP, VOIDP], INT],
      rw_browser_new_page: [[VOIDP, VOIDP], INT],
      rw_browser_close: [[VOIDP], INT],
      rw_browser_ws_endpoint: [[VOIDP], VOIDP],
      rw_browser_free: [[VOIDP], VOID],
      rw_page_target_id: [[VOIDP], VOIDP],
      rw_page_goto: [[VOIDP, VOIDP, VOIDP, DOUBLE, VOIDP, VOIDP], INT],
      rw_page_click: [[VOIDP, VOIDP, DOUBLE], INT],
      rw_page_fill: [[VOIDP, VOIDP, VOIDP, DOUBLE], INT],
      rw_page_title: [[VOIDP, DOUBLE, VOIDP], INT],
      rw_page_text_content: [[VOIDP, VOIDP, DOUBLE, VOIDP], INT],
      rw_page_evaluate: [[VOIDP, VOIDP, VOIDP, DOUBLE, VOIDP], INT],
      rw_page_screenshot: [[VOIDP, VOIDP, VOIDP, VOIDP], INT],
      rw_page_close: [[VOIDP, DOUBLE, INT], INT],
      rw_page_free: [[VOIDP], VOID]
    }.freeze

    attr_reader :path

    def initialize(path)
      @path = File.expand_path(path)
      @handle = Fiddle.dlopen(@path)
      @functions = {}
      SIGNATURES.each do |name, (arguments, result)|
        @functions[name] = Fiddle::Function.new(@handle[name.to_s], arguments, result)
      end
    rescue Fiddle::DLError => e
      raise Rustwright::Error, "cannot load Rustwright library #{@path}: #{e.message}"
    end

    def call(name, *arguments)
      if arguments.any? { |argument| argument.is_a?(String) && argument.include?("\0") }
        raise Rustwright::Error, 'strings passed to the C ABI cannot contain NUL'
      end

      @functions.fetch(name).call(*arguments)
    end

    # This must be invoked before any other ABI call after a failing status.
    def check_status!(status, operation)
      return if status.zero?

      raise Rustwright::Error, last_error_immediately(operation, status)
    end

    # This must be invoked immediately after a direct pointer return is NULL.
    def raise_null_error!(operation)
      raise Rustwright::Error, last_error_immediately(operation, nil)
    end

    def pointer_slot
      slot = Fiddle::Pointer.malloc(Fiddle::SIZEOF_VOIDP)
      slot[0, Fiddle::SIZEOF_VOIDP] = [0].pack('J')
      slot
    end

    def size_slot
      slot = Fiddle::Pointer.malloc(Fiddle::SIZEOF_SIZE_T)
      slot[0, Fiddle::SIZEOF_SIZE_T] = [0].pack(size_t_pack)
      slot
    end

    def pointer_address(slot)
      slot[0, Fiddle::SIZEOF_VOIDP].unpack('J').first
    end

    def size_value(slot)
      slot[0, Fiddle::SIZEOF_SIZE_T].unpack(size_t_pack).first
    end

    def null?(pointer)
      pointer.nil? || pointer.to_i.zero?
    end

    def copy_owned_string(pointer, nullable: false)
      if null?(pointer)
        return nil if nullable

        raise Rustwright::Error, 'Rustwright returned an unexpected NULL string'
      end

      address = pointer.to_i
      begin
        Fiddle::Pointer.new(address).to_s.dup.force_encoding(Encoding::UTF_8)
      ensure
        call(:rw_string_free, address)
      end
    end

    def copy_owned_bytes(pointer, length)
      address = pointer.to_i
      if address.zero?
        # The ABI transfers even the empty NULL/0 pair; release that exact pair.
        call(:rw_bytes_free, address, length)
        return ''.b if length.zero?

        raise Rustwright::Error, "Rustwright returned NULL bytes with length #{length}"
      end

      begin
        Fiddle::Pointer.new(address).to_str(length).dup.force_encoding(Encoding::BINARY)
      ensure
        call(:rw_bytes_free, address, length)
      end
    end

    private

    def last_error_immediately(operation, status)
      borrowed = call(:rw_last_error)
      message = if null?(borrowed)
                  'no native error message was provided'
                else
                  Fiddle::Pointer.new(borrowed.to_i).to_s.dup.force_encoding(Encoding::UTF_8)
                end
      suffix = status.nil? ? '' : " (status #{status})"
      "#{operation} failed#{suffix}: #{message}"
    end

    def size_t_pack
      case Fiddle::SIZEOF_SIZE_T
      when 8 then 'Q'
      when 4 then 'L'
      else
        raise Rustwright::Error, "unsupported size_t width: #{Fiddle::SIZEOF_SIZE_T}"
      end
    end
  end
end
