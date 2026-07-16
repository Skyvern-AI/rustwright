# frozen_string_literal: true

require 'json'
require_relative '../rustwright'
require_relative 'manifest'

module Rustwright
  class AssertionError < Error; end

  class Runner
    def initialize(manifest:, library_path:, case_ids: nil)
      @manifest = manifest
      @library_path = library_path
      @case_ids = case_ids
    end

    def run
      cases = selected_cases
      browser = Rustwright.chromium(library_path: @library_path).launch(headless: true)
      results = []
      close_error = nil

      begin
        cases.each { |benchmark_case| results << run_case(browser, benchmark_case) }
      ensure
        begin
          browser.close
        rescue StandardError => e
          close_error = e
        end
      end

      if close_error
        result = results.last
        if result
          result['ok'] = false
          result['error'] ||= "browser close: #{close_error.message}"
        else
          raise close_error
        end
      end

      { 'lang' => 'ruby', 'results' => results }
    end

    private

    def selected_cases
      cases = @manifest['cases']
      return cases if @case_ids.nil?

      available = cases.each_with_object({}) { |item, ids| ids[item['id']] = true }
      unknown = @case_ids.reject { |id| available.key?(id) }
      unless unknown.empty?
        raise ManifestError, "unknown requested case id(s): #{unknown.join(', ')}"
      end

      selected = @case_ids.each_with_object({}) { |id, ids| ids[id] = true }
      cases.select { |item| selected.key?(item['id']) }
    end

    def run_case(browser, benchmark_case)
      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      captures = {}
      error = nil
      page = nil

      begin
        page = browser.new_page
        benchmark_case['steps'].each_with_index do |step, index|
          begin
            execute_step(page, benchmark_case, step, captures)
          rescue StandardError => e
            error = "step #{index + 1}: #{e.message}"
            break
          end
        end
      rescue StandardError => e
        error = "page creation: #{e.message}"
      ensure
        if page
          begin
            page.close
          rescue StandardError => e
            error ||= "page close: #{e.message}"
          end
        end
      end

      elapsed_ms = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000.0
      result = {
        'id' => benchmark_case['id'],
        'ok' => error.nil?,
        'captures' => captures,
        'ms' => elapsed_ms.round(3)
      }
      result['error'] = error unless error.nil?
      result
    end

    def execute_step(page, benchmark_case, step, captures)
      case step['op']
      when 'goto'
        url = step['useCaseHtml'] ? Rustwright.inline_html_url(benchmark_case['html']) : step['url']
        page.goto(url, wait_until: step['waitUntil'])
      when 'click'
        page.click(step['selector'])
      when 'fill'
        page.fill(step['selector'], step['value'])
      when 'title'
        captures[step['capture']] = page.title
      when 'textContent'
        captures[step['capture']] = page.text_content(step['selector'])
      when 'evaluate'
        value = if step.key?('arg')
                  page.evaluate(step['expression'], step['arg'])
                else
                  page.evaluate(step['expression'])
                end
        captures[step['capture']] = value
      when 'screenshot'
        captures[step['capture']] = page.screenshot.bytesize
      when 'assertTitle'
        assert_string(page.title, step, 'title')
      when 'assertText'
        assert_string(page.text_content(step['selector']), step, "textContent for #{step['selector'].inspect}")
      when 'assertEval'
        actual = page.evaluate(step['expression'])
        return if actual == step['equals']

        raise AssertionError, "expected evaluation #{step['equals'].inspect}, got #{actual.inspect}"
      else
        # Validation makes this unreachable, but retain a defensive boundary.
        raise ManifestError, "unknown operation #{step['op'].inspect}"
      end
    end

    def assert_string(actual, step, label)
      raise AssertionError, "expected #{label} to be a string, got null" if actual.nil?

      if step.key?('equals')
        return if actual == step['equals']

        raise AssertionError, "expected #{label} #{step['equals'].inspect}, got #{actual.inspect}"
      end

      return if actual.include?(step['contains'])

      raise AssertionError, "expected #{label} to contain #{step['contains'].inspect}, got #{actual.inspect}"
    end
  end
end
