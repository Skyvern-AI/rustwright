#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'optparse'
require 'tmpdir'
require_relative 'lib/rustwright'

options = { library_path: Rustwright.default_library_path }
OptionParser.new do |parser|
  parser.banner = 'Usage: ruby ruby/smoke.rb [--lib PATH]'
  parser.on('--lib PATH', 'Exact rustwright C API dynamic-library path') do |path|
    options[:library_path] = path
  end
end.parse!(ARGV)
raise OptionParser::InvalidArgument, "unexpected arguments: #{ARGV.join(' ')}" unless ARGV.empty?

html = <<~HTML
  <!doctype html>
  <html>
    <head><title>Rustwright Ruby Smoke</title></head>
    <body>
      <h1 id="message">ready</h1>
      <input id="name" />
      <button id="go" onclick="document.querySelector('#message').textContent = document.querySelector('#name').value">Go</button>
    </body>
  </html>
HTML

screenshot_path = File.join(Dir.tmpdir, "rustwright-ruby-smoke-#{Process.pid}.png")
browser = nil
page = nil
record = nil

begin
  browser = Rustwright.chromium(library_path: options[:library_path]).launch(headless: true)
  page = browser.new_page
  page.goto(Rustwright.inline_html_url(html))
  title = page.title
  before = page.text_content('#message')
  page.fill('#name', 'Rustwright for Ruby')
  page.click('#go')
  after = page.text_content('#message')
  value = page.evaluate("document.querySelector('#name').value")
  screenshot = page.screenshot(path: screenshot_path)
  record = {
    title: title,
    before: before,
    after: after,
    value: value,
    screenshotBytes: screenshot.bytesize
  }
ensure
  begin
    page.close if page && !page.closed?
  ensure
    browser.close if browser && !browser.closed?
  end
end

puts JSON.generate(record)
