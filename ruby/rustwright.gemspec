# frozen_string_literal: true

require_relative 'lib/rustwright'

Gem::Specification.new do |spec|
  spec.name = 'rustwright'
  spec.version = Rustwright::VERSION
  spec.authors = ['Rustwright contributors']
  spec.summary = 'Ruby bindings for the Rustwright browser automation C API'
  spec.homepage = 'https://github.com/Skyvern-AI/rustwright'
  spec.license = 'MIT'
  spec.required_ruby_version = '>= 2.6'
  spec.platform = ENV.fetch('RUSTWRIGHT_GEM_PLATFORM', Gem::Platform::RUBY)
  spec.files = Dir['lib/**/*.rb', 'lib/rustwright/native/**/*', 'README.md']
  spec.require_paths = ['lib']
end
