# frozen_string_literal: true

require 'time'
require 'uri'

module Rustwright
  class JavaScriptError < StandardError
    attr_reader :javascript_name, :javascript_stack

    def initialize(value)
      @javascript_name = value['name'] || 'Error'
      @javascript_stack = value['stack'] || ''
      super(value['message'] || '')
    end
  end

  module Wire
    module_function

    MARKER = '__rustwright_cdp_unserializable_value__'

    def decode(value)
      decode_value(value, {})
    end

    def decode_value(value, references)
      case value
      when Array
        value.map { |item| decode_value(item, references) }
      when Hash
        decode_hash(value, references)
      else
        value
      end
    end
    private_class_method :decode_value

    def decode_hash(value, references)
      if value.key?('__rustwright_cdp_array__')
        target = []
        references[value['__rustwright_cdp_array__']] = target
        target.concat(Array(value['items']).map { |item| decode_value(item, references) })
        return target
      end

      if value.key?('__rustwright_cdp_object__')
        target = {}
        references[value['__rustwright_cdp_object__']] = target
        Hash(value['entries']).each do |key, item|
          target[key] = decode_value(item, references)
        end
        return target
      end

      if value.key?('__rustwright_cdp_ref__')
        return references[value['__rustwright_cdp_ref__']]
      end

      return nil if value.key?('__rustwright_cdp_undefined__')
      return nil if value.key?('__rustwright_cdp_symbol__')
      return nil if value.key?('__rustwright_cdp_function__')

      if value.key?(MARKER)
        return decode_unserializable(value[MARKER])
      end

      if value.key?('__rustwright_cdp_date__')
        return Time.iso8601(value['__rustwright_cdp_date__'])
      end

      if value.key?('__rustwright_cdp_regexp__')
        regexp = value['__rustwright_cdp_regexp__']
        return Regexp.new(regexp['p'], regexp_options(regexp['f'].to_s))
      end

      if value.key?('__rustwright_cdp_url__')
        return URI.parse(value['__rustwright_cdp_url__'])
      end

      if value.key?('__rustwright_cdp_error__')
        return JavaScriptError.new(value['__rustwright_cdp_error__'])
      end

      value.each_with_object({}) do |(key, item), decoded|
        decoded[key] = decode_value(item, references)
      end
    end
    private_class_method :decode_hash

    def decode_unserializable(value)
      case value
      when 'NaN' then Float::NAN
      when 'Infinity' then Float::INFINITY
      when '-Infinity' then -Float::INFINITY
      when '-0' then -0.0
      else
        value.is_a?(String) && value.match?(/\A-?\d+n\z/) ? value[0...-1].to_i : value
      end
    end
    private_class_method :decode_unserializable

    def regexp_options(flags)
      options = 0
      options |= Regexp::IGNORECASE if flags.include?('i')
      # JavaScript's dotAll flag is Ruby's multiline option.
      options |= Regexp::MULTILINE if flags.include?('s')
      options
    end
    private_class_method :regexp_options
  end
end
