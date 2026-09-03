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

    NODE_NULL = 0
    NODE_BOOL = 1
    NODE_SIGNED = 2
    NODE_UNSIGNED = 3
    NODE_FLOAT = 4
    NODE_STRING = 5
    NODE_ARRAY = 6
    NODE_OBJECT = 7
    NODE_LEAF = 8

    LEAF_UNSERIALIZABLE = 0
    LEAF_BIGINT = 1
    LEAF_DATE = 2
    LEAF_REGEXP = 3
    LEAF_URL = 4
    LEAF_ERROR = 5
    LEAF_UNDEFINED = 6
    LEAF_SYMBOL = 7
    LEAF_FUNCTION = 8

    def decode(native, wire_json)
      unless wire_json.is_a?(String)
        raise ArgumentError, 'wire JSON must be a String'
      end

      graph_slot = native.pointer_slot
      status = native.call(:rw_wire_graph_parse, wire_json, graph_slot)
      native.check_status!(status, 'rw_wire_graph_parse')
      graph = native.pointer_address(graph_slot)
      raise Rustwright::Error, 'rw_wire_graph_parse succeeded without returning a graph' if graph.zero?

      begin
        materialize(native, graph)
      ensure
        native.call(:rw_wire_graph_free, graph)
      end
    end

    def materialize(native, graph)
      count = read_size(native, :rw_wire_graph_node_count, [graph], 'rw_wire_graph_node_count')
      values = Array.new(count)
      kinds = Array.new(count)

      count.times do |node|
        kind = read_int32(
          native,
          :rw_wire_graph_node_kind,
          [graph, node],
          'rw_wire_graph_node_kind'
        )
        kinds[node] = kind
        values[node] = allocate_node(native, graph, node, kind)
      end

      count.times do |node|
        case kinds[node]
        when NODE_ARRAY
          length = read_size(
            native,
            :rw_wire_graph_array_length,
            [graph, node],
            'rw_wire_graph_array_length'
          )
          length.times do |index|
            child = read_size(
              native,
              :rw_wire_graph_array_child,
              [graph, node, index],
              'rw_wire_graph_array_child'
            )
            values[node] << values.fetch(child)
          end
        when NODE_OBJECT
          length = read_size(
            native,
            :rw_wire_graph_object_length,
            [graph, node],
            'rw_wire_graph_object_length'
          )
          length.times do |index|
            key = read_text(
              native,
              :rw_wire_graph_object_key,
              [graph, node, index],
              'rw_wire_graph_object_key'
            )
            child = read_size(
              native,
              :rw_wire_graph_object_child,
              [graph, node, index],
              'rw_wire_graph_object_child'
            )
            values[node][key] = values.fetch(child)
          end
        end
      end

      root = read_size(native, :rw_wire_graph_root, [graph], 'rw_wire_graph_root')
      values.fetch(root)
    end
    private_class_method :materialize

    def allocate_node(native, graph, node, kind)
      case kind
      when NODE_NULL
        nil
      when NODE_BOOL
        read_int32(native, :rw_wire_graph_get_bool, [graph, node], 'rw_wire_graph_get_bool') != 0
      when NODE_SIGNED
        read_int64(native, :rw_wire_graph_get_signed, [graph, node], 'rw_wire_graph_get_signed')
      when NODE_UNSIGNED
        read_uint64(native, :rw_wire_graph_get_unsigned, [graph, node], 'rw_wire_graph_get_unsigned')
      when NODE_FLOAT
        read_double(native, :rw_wire_graph_get_float, [graph, node], 'rw_wire_graph_get_float')
      when NODE_STRING
        read_text(native, :rw_wire_graph_get_string, [graph, node], 'rw_wire_graph_get_string')
      when NODE_ARRAY
        []
      when NODE_OBJECT
        {}
      when NODE_LEAF
        decode_leaf(native, graph, node)
      else
        raise Rustwright::Error, "unknown wire node kind #{kind}"
      end
    end
    private_class_method :allocate_node

    def decode_leaf(native, graph, node)
      kind = read_int32(native, :rw_wire_graph_leaf_kind, [graph, node], 'rw_wire_graph_leaf_kind')
      fields_count = read_size(
        native,
        :rw_wire_graph_leaf_field_count,
        [graph, node],
        'rw_wire_graph_leaf_field_count'
      )
      fields = fields_count.times.map do |index|
        read_text(
          native,
          :rw_wire_graph_leaf_field,
          [graph, node, index],
          'rw_wire_graph_leaf_field'
        )
      end

      case kind
      when LEAF_UNSERIALIZABLE
        decode_unserializable(fields.fetch(0))
      when LEAF_BIGINT
        decode_bigint(fields.fetch(0))
      when LEAF_DATE
        Time.iso8601(fields.fetch(0))
      when LEAF_REGEXP
        Regexp.new(fields.fetch(0), regexp_options(fields.fetch(1)))
      when LEAF_URL
        URI.parse(fields.fetch(0))
      when LEAF_ERROR
        JavaScriptError.new(
          'name' => fields.fetch(0),
          'message' => fields.fetch(1),
          'stack' => fields.fetch(2)
        )
      when LEAF_UNDEFINED, LEAF_SYMBOL, LEAF_FUNCTION
        nil
      else
        raise Rustwright::Error, "unknown wire leaf kind #{kind}"
      end
    end
    private_class_method :decode_leaf

    def decode_unserializable(value)
      case value
      when 'NaN' then Float::NAN
      when 'Infinity' then Float::INFINITY
      when '-Infinity' then -Float::INFINITY
      when '-0' then -0.0
      else
        value
      end
    end
    private_class_method :decode_unserializable

    def decode_bigint(value)
      Integer(value, 10)
    rescue ArgumentError
      value
    end
    private_class_method :decode_bigint

    def regexp_options(flags)
      options = 0
      options |= Regexp::IGNORECASE if flags.include?('i')
      # JavaScript's dotAll flag is Ruby's multiline option.
      options |= Regexp::MULTILINE if flags.include?('s')
      options
    end
    private_class_method :regexp_options

    def read_size(native, name, arguments, operation)
      slot = native.size_slot
      status = native.call(name, *arguments, slot)
      native.check_status!(status, operation)
      native.size_value(slot)
    end
    private_class_method :read_size

    def read_int32(native, name, arguments, operation)
      slot = native.int32_slot
      status = native.call(name, *arguments, slot)
      native.check_status!(status, operation)
      native.int32_value(slot)
    end
    private_class_method :read_int32

    def read_int64(native, name, arguments, operation)
      slot = native.int64_slot
      status = native.call(name, *arguments, slot)
      native.check_status!(status, operation)
      native.int64_value(slot)
    end
    private_class_method :read_int64

    def read_uint64(native, name, arguments, operation)
      slot = native.uint64_slot
      status = native.call(name, *arguments, slot)
      native.check_status!(status, operation)
      native.uint64_value(slot)
    end
    private_class_method :read_uint64

    def read_double(native, name, arguments, operation)
      slot = native.double_slot
      status = native.call(name, *arguments, slot)
      native.check_status!(status, operation)
      native.double_value(slot)
    end
    private_class_method :read_double

    def read_text(native, name, arguments, operation)
      pointer_slot = native.pointer_slot
      length_slot = native.size_slot
      status = native.call(name, *arguments, pointer_slot, length_slot)
      native.check_status!(status, operation)
      native.copy_borrowed_bytes(
        native.pointer_address(pointer_slot),
        native.size_value(length_slot)
      ).force_encoding(Encoding::UTF_8)
    end
    private_class_method :read_text
  end
end
