# frozen_string_literal: true

require 'minitest/autorun'
require_relative '../lib/rustwright'
require_relative '../lib/rustwright/manifest'

class RustwrightContractTest < Minitest::Test
  def test_canonical_inline_html_url
    assert_equal(
      'data:text/html;charset=utf-8,A-z_.~%20%2B%C3%A9',
      Rustwright.inline_html_url("A-z_.~ +é")
    )
  end

  def test_launch_and_screenshot_option_normalization
    assert_equal(
      {
        'headless' => true,
        'executable_path' => '/chromium',
        'ignore_default_args' => ['--one'],
        'chromium_sandbox' => false
      },
      Rustwright::Normalize.launch(
        headless: true,
        executablePath: '/chromium',
        ignore_default_args: ['--one'],
        chromiumSandbox: false
      )
    )
    assert_equal(
      { 'fullPage' => true, 'omitBackground' => true, 'type' => 'png' },
      Rustwright::Normalize.screenshot(full_page: true, omitBackground: true, type: :png)
    )
  end
  def test_wire_graph_materializes_identity_cycles_and_embedded_nul
    library_path = ENV.fetch('RUSTWRIGHT_CAPI_LIB', Rustwright.default_library_path)
    skip "native library not found at #{library_path}" unless File.file?(library_path)

    native = Rustwright::Native.new(library_path)
    decoded = Rustwright::Wire.decode(
      native,
      <<~JSON
        {
          "__rustwright_cdp_object__": 1,
          "entries": {
            "self": {"__rustwright_cdp_ref__": 1},
            "shared": {
              "__rustwright_cdp_object__": 2,
              "entries": {"value": "a\\u0000b"}
            },
            "again": {"__rustwright_cdp_ref__": 2},
            "bigint": {"__rustwright_cdp_bigint__": "123n"},
            "malformed_bigint": {"__rustwright_cdp_bigint__": "not-a-number"}
          }
        }
      JSON
    )

    assert_same decoded, decoded.fetch('self')
    assert_same decoded.fetch('shared'), decoded.fetch('again')
    assert_equal "a\0b", decoded.dig('shared', 'value')
    assert_equal 123, decoded.fetch('bigint')
    assert_equal 'not-a-number', decoded.fetch('malformed_bigint')
  end

  def test_smoke_manifest_is_valid
    manifest_path = File.expand_path('../../bindings/cases/smoke.json', __dir__)
    assert_equal 5, Rustwright::Manifest.load(manifest_path)['cases'].length
  end

  def test_duplicate_capture_is_rejected
    manifest = {
      'version' => 1,
      'cases' => [{
        'id' => 'duplicate',
        'steps' => [
          { 'op' => 'title', 'capture' => 'same' },
          { 'op' => 'screenshot', 'capture' => 'same' }
        ]
      }]
    }

    error = assert_raises(Rustwright::ManifestError) do
      Rustwright::Manifest.validate!(manifest)
    end
    assert_match(/duplicate capture/, error.message)
  end

  def test_unknown_operation_is_rejected
    manifest = {
      'version' => 1,
      'cases' => [{
        'id' => 'unknown',
        'steps' => [{ 'op' => 'hover', 'selector' => '#thing' }]
      }]
    }

    error = assert_raises(Rustwright::ManifestError) do
      Rustwright::Manifest.validate!(manifest)
    end
    assert_match(/unknown operation/, error.message)
  end

  def test_fill_rejects_nul_before_calling_c_abi_and_browser_closes
    library_path = Rustwright.default_library_path
    skip "native library not found at #{library_path}" unless File.file?(library_path)

    browser = Rustwright.chromium(library_path: library_path).launch(headless: true)
    page = browser.new_page
    page.goto(Rustwright.inline_html_url('<input id="name">'))

    native = page.instance_variable_get(:@native)
    functions = native.instance_variable_get(:@functions)
    fill_function = functions.fetch(:rw_page_fill)
    abi_called = false
    fill_spy = Object.new
    fill_spy.define_singleton_method(:call) do |*arguments|
      abi_called = true
      fill_function.call(*arguments)
    end
    functions[:rw_page_fill] = fill_spy

    error = assert_raises(Rustwright::Error) { page.fill('#name', "a\0b") }
    assert_equal 'strings passed to the C ABI cannot contain NUL', error.message
    refute abi_called
  ensure
    functions[:rw_page_fill] = fill_function if functions && fill_function
    browser.close if browser
    assert browser.closed? if browser
  end
end
