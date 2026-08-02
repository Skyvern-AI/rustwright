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
        execute_steps(page, benchmark_case, captures)
      rescue StandardError => e
        error = page ? e.message : "page creation: #{e.message}"
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

    def execute_steps(page, benchmark_case, captures)
      steps = benchmark_case['steps']
      repeat = benchmark_case.fetch('repeat', 1)
      return execute_step_block(page, benchmark_case, steps, 0, captures) if repeat == 1

      first_goto = steps.index { |step| step['op'] == 'goto' }
      unless first_goto
        raise ManifestError, "case #{benchmark_case['id'].inspect} has repeat #{repeat} but no goto step"
      end

      execute_step_block(page, benchmark_case, steps[0..first_goto], 0, captures)
      1.upto(repeat) do |iteration|
        iteration_captures = {}
        begin
          execute_step_block(
            page,
            benchmark_case,
            steps[(first_goto + 1)..-1],
            first_goto + 1,
            iteration_captures
          )
        rescue StandardError => e
          captures.merge!(iteration_captures)
          raise Error, "iteration #{iteration}: #{e.message}"
        end
        captures.merge!(iteration_captures)
      end
    end

    def execute_step_block(page, benchmark_case, steps, index_offset, captures)
      steps.each_with_index do |step, index|
        execute_step(page, benchmark_case, step, captures)
      rescue StandardError => e
        raise Error, "step #{index_offset + index + 1}: #{e.message}"
      end
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
        insert_capture(captures, step['capture'], page.title)
      when 'textContent'
        insert_capture(captures, step['capture'], page.text_content(step['selector']))
      when 'evaluate'
        value = if step.key?('arg')
                  page.evaluate(step['expression'], step['arg'])
                else
                  page.evaluate(step['expression'])
                end
        insert_capture(captures, step['capture'], value)
      when 'screenshot'
        insert_capture(captures, step['capture'], page.screenshot.bytesize)
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

    def insert_capture(captures, name, value)
      raise ManifestError, "duplicate capture name #{name.inspect}" if captures.key?(name)

      captures[name] = value
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
