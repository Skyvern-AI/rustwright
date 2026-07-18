#!/usr/bin/env ruby
# frozen_string_literal: true

require 'fileutils'
require 'rubygems/package'

PLATFORMS = {
  'arm64-darwin' => 'librustwright_capi.dylib',
  'x86_64-darwin' => 'librustwright_capi.dylib',
  'aarch64-linux' => 'librustwright_capi.so',
  'x86_64-linux' => 'librustwright_capi.so'
}.freeze

unless ARGV.length == 2 && PLATFORMS.key?(ARGV[0])
  abort "Usage: ruby ruby/package.rb PLATFORM PATH_TO_NATIVE\n" \
        "Platforms: #{PLATFORMS.keys.join(', ')}"
end

root = __dir__
platform = ARGV[0]
source = File.expand_path(ARGV[1])
abort "Native library not found: #{source}" unless File.file?(source)

stage_root = File.join(root, 'lib', 'rustwright', 'native')
stage_dir = File.join(stage_root, platform)
destination = File.join(stage_dir, PLATFORMS.fetch(platform))
previous_platform = ENV['RUSTWRIGHT_GEM_PLATFORM']

begin
  # Keep each platform gem isolated even when this entrypoint runs repeatedly.
  FileUtils.rm_rf(stage_root)
  FileUtils.mkdir_p(stage_dir)
  FileUtils.cp(source, destination)
  ENV['RUSTWRIGHT_GEM_PLATFORM'] = platform

  Dir.chdir(root) do
    specification = Gem::Specification.load('rustwright.gemspec')
    abort 'Unable to load rustwright.gemspec' unless specification

    gem_file = Gem::Package.build(specification)
    package_dir = File.join(root, 'pkg')
    package_path = File.join(package_dir, File.basename(gem_file))
    FileUtils.mkdir_p(package_dir)
    FileUtils.rm_f(package_path)
    FileUtils.mv(gem_file, package_path)
    puts package_path
  end
ensure
  FileUtils.rm_rf(stage_root)
  if previous_platform
    ENV['RUSTWRIGHT_GEM_PLATFORM'] = previous_platform
  else
    ENV.delete('RUSTWRIGHT_GEM_PLATFORM')
  end
end
