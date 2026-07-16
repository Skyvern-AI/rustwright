# frozen_string_literal: true

require 'json'
require 'uri'
require_relative '../rustwright'

module Rustwright
  class ManifestError < Error; end

  module Manifest
    module_function

    OPERATIONS = %w[
      goto click fill title textContent evaluate screenshot
      assertTitle assertText assertEval
    ].freeze

    CAPTURE_OPERATIONS = %w[title textContent evaluate screenshot].freeze

    def load(path)
      parsed = JSON.parse(File.read(path, encoding: Encoding::UTF_8))
      validate!(parsed)
      parsed
    rescue Errno::ENOENT, Errno::EACCES => e
      raise ManifestError, "cannot read manifest #{path}: #{e.message}"
    rescue JSON::ParserError => e
      raise ManifestError, "invalid manifest JSON: #{e.message}"
    end

    def validate!(manifest)
      object!(manifest, '$')
      keys!(manifest, %w[version cases], %w[version cases], '$')
      raise ManifestError, '$.version must be 1' unless manifest['version'] == 1

      cases = manifest['cases']
      array!(cases, '$.cases')
      raise ManifestError, '$.cases must contain at least one case' if cases.empty?

      ids = {}
      cases.each_with_index do |benchmark_case, index|
        path = "$.cases[#{index}]"
        validate_case!(benchmark_case, path)
        id = benchmark_case['id']
        raise ManifestError, "duplicate case id #{id.inspect}" if ids.key?(id)

        ids[id] = true
      end
      manifest
    end

    def validate_case!(benchmark_case, path)
      object!(benchmark_case, path)
      keys!(benchmark_case, %w[id description html url steps], %w[id steps], path)
      non_empty_string!(benchmark_case['id'], "#{path}.id")
      string!(benchmark_case['description'], "#{path}.description") if benchmark_case.key?('description')
      string!(benchmark_case['html'], "#{path}.html") if benchmark_case.key?('html')
      if benchmark_case.key?('url')
        string!(benchmark_case['url'], "#{path}.url")
        begin
          URI.parse(benchmark_case['url'])
        rescue URI::InvalidURIError => e
          raise ManifestError, "#{path}.url is not a URI reference: #{e.message}"
        end
      end

      steps = benchmark_case['steps']
      array!(steps, "#{path}.steps")
      raise ManifestError, "#{path}.steps must contain at least one step" if steps.empty?

      captures = {}
      steps.each_with_index do |step, index|
        step_path = "#{path}.steps[#{index}]"
        validate_step!(step, step_path, benchmark_case)
        next unless CAPTURE_OPERATIONS.include?(step['op'])

        capture = step['capture']
        if captures.key?(capture)
          raise ManifestError, "duplicate capture #{capture.inspect} in case #{benchmark_case['id'].inspect}"
        end
        captures[capture] = true
      end
    end
    private_class_method :validate_case!

    def validate_step!(step, path, benchmark_case)
      object!(step, path)
      non_empty_string!(step['op'], "#{path}.op") if step.key?('op')
      operation = step['op']
      raise ManifestError, "#{path}.op is required" if operation.nil?
      raise ManifestError, "#{path}.op has unknown operation #{operation.inspect}" unless OPERATIONS.include?(operation)

      case operation
      when 'goto'
        keys!(step, %w[op url useCaseHtml waitUntil], %w[op], path)
        sources = [step.key?('url'), step.key?('useCaseHtml')].count(true)
        raise ManifestError, "#{path} must contain exactly one of url or useCaseHtml" unless sources == 1
        non_empty_string!(step['url'], "#{path}.url") if step.key?('url')
        if step.key?('useCaseHtml')
          raise ManifestError, "#{path}.useCaseHtml must be true" unless step['useCaseHtml'] == true
          unless benchmark_case.key?('html')
            raise ManifestError, "#{path}.useCaseHtml requires case html"
          end
        end
        if step.key?('waitUntil')
          allowed = %w[load domcontentloaded networkidle commit]
          unless allowed.include?(step['waitUntil'])
            raise ManifestError, "#{path}.waitUntil must be one of #{allowed.join(', ')}"
          end
        end
      when 'click'
        keys!(step, %w[op selector], %w[op selector], path)
        non_empty_string!(step['selector'], "#{path}.selector")
      when 'fill'
        keys!(step, %w[op selector value], %w[op selector value], path)
        non_empty_string!(step['selector'], "#{path}.selector")
        string!(step['value'], "#{path}.value")
      when 'title'
        capture_step!(step, path, %w[op capture])
      when 'textContent'
        capture_step!(step, path, %w[op selector capture])
        non_empty_string!(step['selector'], "#{path}.selector")
      when 'evaluate'
        capture_step!(step, path, %w[op expression arg capture], %w[op expression capture])
        non_empty_string!(step['expression'], "#{path}.expression")
      when 'screenshot'
        capture_step!(step, path, %w[op capture])
      when 'assertTitle'
        assertion_predicate!(step, path, %w[op equals contains])
      when 'assertText'
        assertion_predicate!(step, path, %w[op selector equals contains], %w[op selector])
        non_empty_string!(step['selector'], "#{path}.selector")
      when 'assertEval'
        keys!(step, %w[op expression equals], %w[op expression equals], path)
        non_empty_string!(step['expression'], "#{path}.expression")
      end
    end
    private_class_method :validate_step!

    def capture_step!(step, path, allowed, required = allowed)
      keys!(step, allowed, required, path)
      non_empty_string!(step['capture'], "#{path}.capture")
    end
    private_class_method :capture_step!

    def assertion_predicate!(step, path, allowed, required = %w[op])
      keys!(step, allowed, required, path)
      predicates = [step.key?('equals'), step.key?('contains')].count(true)
      raise ManifestError, "#{path} must contain exactly one of equals or contains" unless predicates == 1

      string!(step['equals'], "#{path}.equals") if step.key?('equals')
      string!(step['contains'], "#{path}.contains") if step.key?('contains')
    end
    private_class_method :assertion_predicate!

    def keys!(value, allowed, required, path)
      unknown = value.keys - allowed
      missing = required - value.keys
      raise ManifestError, "#{path} has unknown properties: #{unknown.join(', ')}" unless unknown.empty?
      raise ManifestError, "#{path} is missing required properties: #{missing.join(', ')}" unless missing.empty?
    end
    private_class_method :keys!

    def object!(value, path)
      raise ManifestError, "#{path} must be an object" unless value.is_a?(Hash)
    end
    private_class_method :object!

    def array!(value, path)
      raise ManifestError, "#{path} must be an array" unless value.is_a?(Array)
    end
    private_class_method :array!

    def string!(value, path)
      raise ManifestError, "#{path} must be a string" unless value.is_a?(String)
    end
    private_class_method :string!

    def non_empty_string!(value, path)
      string!(value, path)
      raise ManifestError, "#{path} must not be empty" if value.empty?
    end
    private_class_method :non_empty_string!
  end
end
