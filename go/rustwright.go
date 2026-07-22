// Package rustwright provides a Go wrapper for the Rustwright C ABI.
package rustwright

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"runtime"
	"sync"
	"unsafe"
)

// DefaultLibraryPath returns the default path relative to the repository root.
// Run from the repository root, set RUSTWRIGHT_LIB, or pass a path to Open to
// choose another exact library.
func DefaultLibraryPath() string {
	if path := os.Getenv("RUSTWRIGHT_LIB"); path != "" {
		return path
	}
	if runtime.GOOS == "darwin" {
		return "target/release/librustwright_capi.dylib"
	}
	return "target/release/librustwright_capi.so"
}

// Chromium is a loaded Rustwright library and its Chromium entrypoint.
type Chromium struct {
	native *nativeAPI
}

// Open loads the exact dynamic library at path and binds every rw_* symbol.
func Open(path string) (*Chromium, error) {
	native, err := loadNative(path)
	if err != nil {
		return nil, err
	}
	return &Chromium{native: native}, nil
}

// LaunchOptions uses idiomatic camel-case Go fields and is normalized to the
// C core's snake_case JSON wire format.
type LaunchOptions struct {
	Headless             *bool
	ExecutablePath       string
	Channel              string
	Args                 []string
	IgnoreAllDefaultArgs bool
	IgnoreDefaultArgs    []string
	Timeout              *float64
	UserDataDir          string
	Env                  map[string]string
	ChromiumSandbox      bool
	Proxy                *ProxyOptions
}

// Bool returns a pointer to v for optional boolean fields.
func Bool(v bool) *bool {
	return &v
}

type ProxyOptions struct {
	Server   string `json:"server"`
	Bypass   string `json:"bypass,omitempty"`
	Username string `json:"username,omitempty"`
	Password string `json:"password,omitempty"`
}

func (o LaunchOptions) wireJSON() ([]byte, error) {
	wire := make(map[string]any)
	if o.Headless != nil {
		wire["headless"] = *o.Headless
	}
	if o.ExecutablePath != "" {
		wire["executable_path"] = o.ExecutablePath
	}
	if o.Channel != "" {
		wire["channel"] = o.Channel
	}
	if o.Args != nil {
		wire["args"] = o.Args
	}
	if o.IgnoreAllDefaultArgs {
		wire["ignore_all_default_args"] = true
	}
	if o.IgnoreDefaultArgs != nil {
		wire["ignore_default_args"] = o.IgnoreDefaultArgs
	}
	if o.Timeout != nil {
		wire["timeout"] = *o.Timeout
	}
	if o.UserDataDir != "" {
		wire["user_data_dir"] = o.UserDataDir
	}
	if o.Env != nil {
		wire["env"] = o.Env
	}
	if o.ChromiumSandbox {
		wire["chromium_sandbox"] = true
	}
	if o.Proxy != nil {
		wire["proxy"] = o.Proxy
	}
	return json.Marshal(wire)
}

func (c *Chromium) ExecutablePath() (*string, error) {
	var ptr uintptr
	err := c.native.onOSThread(func() int32 {
		return c.native.chromiumExecutablePath(&ptr)
	})
	if err != nil {
		return nil, fmt.Errorf("executable path: %w", err)
	}
	if ptr == 0 {
		return nil, nil
	}
	value := copyCString(ptr)
	c.native.stringFree(ptr)
	return &value, nil
}

func (c *Chromium) Launch(options LaunchOptions) (*Browser, error) {
	encoded, err := options.wireJSON()
	if err != nil {
		return nil, fmt.Errorf("launch options: %w", err)
	}
	optionsBuf, optionsPtr, err := cString(string(encoded))
	if err != nil {
		return nil, err
	}
	var handle uintptr
	err = c.native.onOSThread(func() int32 {
		return c.native.chromiumLaunch(optionsPtr, &handle)
	})
	runtime.KeepAlive(optionsBuf)
	if err != nil {
		return nil, fmt.Errorf("launch Chromium: %w", err)
	}
	if handle == 0 {
		return nil, errors.New("launch Chromium: native call returned a null browser")
	}
	return &Browser{native: c.native, handle: handle}, nil
}

type Browser struct {
	native *nativeAPI
	mu     sync.Mutex
	handle uintptr
	closed bool
}

func (b *Browser) NewPage() (*Page, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed || b.handle == 0 {
		return nil, errors.New("rustwright: browser is closed")
	}
	var handle uintptr
	err := b.native.onOSThread(func() int32 {
		return b.native.browserNewPage(b.handle, &handle)
	})
	if err != nil {
		return nil, fmt.Errorf("new page: %w", err)
	}
	if handle == 0 {
		return nil, errors.New("new page: native call returned a null page")
	}
	return &Page{browser: b, native: b.native, handle: handle}, nil
}

func (b *Browser) WSEndpoint() (string, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed || b.handle == 0 {
		return "", errors.New("rustwright: browser is closed")
	}
	return b.native.directString(func() uintptr {
		return b.native.browserWSEndpoint(b.handle)
	}, "browser WebSocket endpoint")
}

// Close closes Chromium and then frees its opaque handle exactly once.
func (b *Browser) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return nil
	}
	b.closed = true
	handle := b.handle
	b.handle = 0
	if handle == 0 {
		return nil
	}
	err := b.native.onOSThread(func() int32 { return b.native.browserClose(handle) })
	b.native.browserFree(handle)
	if err != nil {
		return fmt.Errorf("close browser: %w", err)
	}
	return nil
}

type GotoOptions struct {
	WaitUntil string
	Timeout   *float64
	Referer   string
}

type ActionOptions struct {
	Timeout *float64
}

type EvaluateOptions struct {
	Timeout *float64
}

type CloseOptions struct {
	Timeout         *float64
	RunBeforeUnload bool
}

type Clip struct {
	X      float64 `json:"x"`
	Y      float64 `json:"y"`
	Width  float64 `json:"width"`
	Height float64 `json:"height"`
}

type ScreenshotOptions struct {
	Path           string   `json:"path,omitempty"`
	FullPage       bool     `json:"fullPage,omitempty"`
	Clip           *Clip    `json:"clip,omitempty"`
	Timeout        *float64 `json:"timeout,omitempty"`
	Type           string   `json:"type,omitempty"`
	Quality        *int     `json:"quality,omitempty"`
	OmitBackground bool     `json:"omitBackground,omitempty"`
}

type Page struct {
	browser *Browser
	native  *nativeAPI
	mu      sync.Mutex
	handle  uintptr
	closed  bool
}

func timeoutOrNaN(timeout *float64) float64 {
	if timeout == nil {
		return math.NaN()
	}
	return *timeout
}

// withHandle holds the parent browser and page locks for the full native call.
func (p *Page) withHandle(call func(uintptr) error) error {
	p.browser.mu.Lock()
	defer p.browser.mu.Unlock()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || p.handle == 0 {
		return errors.New("rustwright: page is closed")
	}
	if p.browser.closed {
		return errors.New("rustwright: browser is closed")
	}
	return call(p.handle)
}

func (p *Page) TargetID() (string, error) {
	var value string
	err := p.withHandle(func(handle uintptr) error {
		decoded, err := p.native.directString(func() uintptr {
			return p.native.pageTargetID(handle)
		}, "page target id")
		value = decoded
		return err
	})
	return value, err
}

func (p *Page) Goto(rawURL string, options *GotoOptions) (any, error) {
	urlBuf, urlPtr, err := cString(rawURL)
	if err != nil {
		return nil, err
	}
	var waitUntil, referer string
	var timeout *float64
	if options != nil {
		waitUntil, referer, timeout = options.WaitUntil, options.Referer, options.Timeout
	}
	waitBuf, waitPtr, err := optionalCString(waitUntil, waitUntil != "")
	if err != nil {
		return nil, err
	}
	refererBuf, refererPtr, err := optionalCString(referer, referer != "")
	if err != nil {
		return nil, err
	}
	var result any
	err = p.withHandle(func(handle uintptr) error {
		var ptr uintptr
		callErr := p.native.onOSThread(func() int32 {
			return p.native.pageGoto(handle, urlPtr, waitPtr, timeoutOrNaN(timeout), refererPtr, &ptr)
		})
		runtime.KeepAlive(urlBuf)
		runtime.KeepAlive(waitBuf)
		runtime.KeepAlive(refererBuf)
		if callErr != nil {
			return fmt.Errorf("goto %q: %w", rawURL, callErr)
		}
		if ptr == 0 {
			result = nil
			return nil
		}
		data := []byte(copyCString(ptr))
		p.native.stringFree(ptr)
		if err := json.Unmarshal(data, &result); err != nil {
			return fmt.Errorf("decode goto response: %w", err)
		}
		return nil
	})
	return result, err
}

func (p *Page) Click(selector string, options *ActionOptions) error {
	selectorBuf, selectorPtr, err := cString(selector)
	if err != nil {
		return err
	}
	var timeout *float64
	if options != nil {
		timeout = options.Timeout
	}
	return p.withHandle(func(handle uintptr) error {
		err := p.native.onOSThread(func() int32 {
			return p.native.pageClick(handle, selectorPtr, timeoutOrNaN(timeout))
		})
		runtime.KeepAlive(selectorBuf)
		if err != nil {
			return fmt.Errorf("click %q: %w", selector, err)
		}
		return nil
	})
}

func (p *Page) Fill(selector, value string, options *ActionOptions) error {
	selectorBuf, selectorPtr, err := cString(selector)
	if err != nil {
		return err
	}
	valueBuf, valuePtr, err := cString(value)
	if err != nil {
		return err
	}
	var timeout *float64
	if options != nil {
		timeout = options.Timeout
	}
	return p.withHandle(func(handle uintptr) error {
		err := p.native.onOSThread(func() int32 {
			return p.native.pageFill(handle, selectorPtr, valuePtr, timeoutOrNaN(timeout))
		})
		runtime.KeepAlive(selectorBuf)
		runtime.KeepAlive(valueBuf)
		if err != nil {
			return fmt.Errorf("fill %q: %w", selector, err)
		}
		return nil
	})
}

func (p *Page) Title(options *ActionOptions) (string, error) {
	var value string
	var timeout *float64
	if options != nil {
		timeout = options.Timeout
	}
	err := p.withHandle(func(handle uintptr) error {
		var ptr uintptr
		err := p.native.onOSThread(func() int32 {
			return p.native.pageTitle(handle, timeoutOrNaN(timeout), &ptr)
		})
		if err != nil {
			return fmt.Errorf("title: %w", err)
		}
		if ptr == 0 {
			return errors.New("title: native call returned a null string")
		}
		value = copyCString(ptr)
		p.native.stringFree(ptr)
		return nil
	})
	return value, err
}

func (p *Page) TextContent(selector string, options *ActionOptions) (*string, error) {
	selectorBuf, selectorPtr, err := cString(selector)
	if err != nil {
		return nil, err
	}
	var timeout *float64
	if options != nil {
		timeout = options.Timeout
	}
	var value *string
	err = p.withHandle(func(handle uintptr) error {
		var ptr uintptr
		err := p.native.onOSThread(func() int32 {
			return p.native.pageTextContent(handle, selectorPtr, timeoutOrNaN(timeout), &ptr)
		})
		runtime.KeepAlive(selectorBuf)
		if err != nil {
			return fmt.Errorf("text content %q: %w", selector, err)
		}
		if ptr != 0 {
			decoded := copyCString(ptr)
			p.native.stringFree(ptr)
			value = &decoded
		}
		return nil
	})
	return value, err
}

// Evaluate encodes arg as one JSON value. A nil arg means the optional ABI
// argument is absent; JavaScript null can be sent with json.RawMessage("null").
func (p *Page) Evaluate(expression string, arg any, options *EvaluateOptions) (any, error) {
	expressionBuf, expressionPtr, err := cString(expression)
	if err != nil {
		return nil, err
	}
	var argBuf []byte
	var argPtr *byte
	if arg != nil {
		encoded, err := json.Marshal(arg)
		if err != nil {
			return nil, fmt.Errorf("evaluate argument: %w", err)
		}
		argBuf, argPtr, err = cString(string(encoded))
		if err != nil {
			return nil, err
		}
	}
	var timeout *float64
	if options != nil {
		timeout = options.Timeout
	}
	var value any
	err = p.withHandle(func(handle uintptr) error {
		var ptr uintptr
		err := p.native.onOSThread(func() int32 {
			return p.native.pageEvaluate(handle, expressionPtr, argPtr, timeoutOrNaN(timeout), &ptr)
		})
		runtime.KeepAlive(expressionBuf)
		runtime.KeepAlive(argBuf)
		if err != nil {
			return fmt.Errorf("evaluate: %w", err)
		}
		if ptr == 0 {
			return errors.New("evaluate: native call returned a null string")
		}
		data := []byte(copyCString(ptr))
		p.native.stringFree(ptr)
		value, err = decodeEvaluateJSON(data)
		return err
	})
	return value, err
}

func (p *Page) Screenshot(options *ScreenshotOptions) ([]byte, error) {
	var optionsBuf []byte
	var optionsPtr *byte
	if options != nil {
		encoded, err := json.Marshal(options)
		if err != nil {
			return nil, fmt.Errorf("screenshot options: %w", err)
		}
		optionsBuf, optionsPtr, err = cString(string(encoded))
		if err != nil {
			return nil, err
		}
	}
	var output []byte
	err := p.withHandle(func(handle uintptr) error {
		var ptr, length uintptr
		err := p.native.onOSThread(func() int32 {
			return p.native.pageScreenshot(handle, optionsPtr, &ptr, &length)
		})
		runtime.KeepAlive(optionsBuf)
		if err != nil {
			return fmt.Errorf("screenshot: %w", err)
		}
		if ptr == 0 {
			if length != 0 {
				return fmt.Errorf("screenshot: native call returned null with length %d", length)
			}
			p.native.bytesFree(ptr, length)
			output = []byte{}
			return nil
		}
		output = append([]byte(nil), unsafe.Slice((*byte)(pointerFromUintptr(ptr)), length)...)
		p.native.bytesFree(ptr, length)
		return nil
	})
	return output, err
}

// Close closes the page and then frees its opaque handle exactly once.
func (p *Page) Close(options *CloseOptions) error {
	p.browser.mu.Lock()
	defer p.browser.mu.Unlock()
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return nil
	}
	p.closed = true
	handle := p.handle
	p.handle = 0
	if handle == 0 {
		return nil
	}
	var timeout *float64
	runBeforeUnload := int32(0)
	if options != nil {
		timeout = options.Timeout
		if options.RunBeforeUnload {
			runBeforeUnload = 1
		}
	}
	err := p.native.onOSThread(func() int32 {
		return p.native.pageClose(handle, timeoutOrNaN(timeout), runBeforeUnload)
	})
	p.native.pageFree(handle)
	if err != nil {
		return fmt.Errorf("close page: %w", err)
	}
	return nil
}
