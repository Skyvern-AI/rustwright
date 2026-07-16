#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'optparse'
require_relative 'lib/rustwright'
require_relative 'lib/rustwright/manifest'
require_relative 'lib/rustwright/runner'

argv = ARGV.dup
argv.shift if argv.first == '--'
options = {}

parser = OptionParser.new do |config|
  config.banner = 'Usage: ruby ruby/runner.rb --manifest PATH --lib PATH --out PATH [--cases id1,id2]'
  config.on('--manifest PATH', 'Benchmark manifest JSON') { |path| options[:manifest_path] = path }
  config.on('--lib PATH', 'Exact rustwright C API dynamic-library path') { |path| options[:library_path] = path }
  config.on('--out PATH', 'Results JSON output path') { |path| options[:out_path] = path }
  config.on('--cases IDS', 'Comma-separated exact case ids') { |ids| options[:case_ids_raw] = ids }
end

begin
  parser.parse!(argv)
  raise OptionParser::InvalidArgument, "unexpected arguments: #{argv.join(' ')}" unless argv.empty?
  {
    manifest_path: '--manifest',
    library_path: '--lib',
    out_path: '--out'
  }.each do |key, flag|
    raise OptionParser::MissingArgument, flag unless options[key]
  end

  case_ids = nil
  if options.key?(:case_ids_raw)
    case_ids = options[:case_ids_raw].split(',', -1)
    raise OptionParser::InvalidArgument, '--cases cannot contain an empty id' if case_ids.any?(&:empty?)
    duplicates = case_ids.group_by(&:itself).select { |_id, values| values.length > 1 }.keys
    unless duplicates.empty?
      raise OptionParser::InvalidArgument, "--cases contains duplicate id(s): #{duplicates.join(', ')}"
    end
  end

  manifest = Rustwright::Manifest.load(options[:manifest_path])
  document = Rustwright::Runner.new(
    manifest: manifest,
    library_path: options[:library_path],
    case_ids: case_ids
  ).run
  File.write(options[:out_path], JSON.pretty_generate(document) + "\n")
  puts JSON.generate(document)
  exit(document['results'].all? { |result| result['ok'] } ? 0 : 1)
rescue OptionParser::ParseError, Rustwright::ManifestError, Rustwright::Error, SystemCallError => e
  warn "ruby runner: #{e.message}"
  warn parser
  exit 2
end
