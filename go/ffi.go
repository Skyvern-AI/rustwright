package rustwright

import (
	"errors"
	"fmt"
	"runtime"
	"sync"
	"unsafe"

	"github.com/ebitengine/purego"
)

// nativeAPI is the complete rustwright.h ABI. Opaque C pointers are uintptrs;
// buffers passed by Go remain live until their synchronous native call returns.
type nativeAPI struct {
	handle uintptr

	lastError              func() uintptr
	stringFree             func(uintptr)
	bytesFree              func(uintptr, uintptr)
	decodeWire             func(*byte, *uintptr) int32
	chromiumExecutablePath func(*uintptr) int32
	chromiumLaunch         func(*byte, *uintptr) int32
	browserNewPage         func(uintptr, *uintptr) int32
	browserClose           func(uintptr) int32
	browserWSEndpoint      func(uintptr) uintptr
	browserFree            func(uintptr)
	pageTargetID           func(uintptr) uintptr
	pageGoto               func(uintptr, *byte, *byte, float64, *byte, *uintptr) int32
	pageClick              func(uintptr, *byte, float64) int32
	pageFill               func(uintptr, *byte, *byte, float64) int32
	pageTitle              func(uintptr, float64, *uintptr) int32
	pageTextContent        func(uintptr, *byte, float64, *uintptr) int32
	pageEvaluate           func(uintptr, *byte, *byte, float64, *uintptr) int32
	pageScreenshot         func(uintptr, *byte, *uintptr, *uintptr) int32
	pageClose              func(uintptr, float64, int32) int32
	pageFree               func(uintptr)
}

var wireDecodeNative struct {
	sync.RWMutex
	native *nativeAPI
}

func loadNative(path string) (_ *nativeAPI, err error) {
	if path == "" {
		return nil, errors.New("rustwright: shared library path is empty")
	}
	h, err := purego.Dlopen(path, purego.RTLD_NOW|purego.RTLD_LOCAL)
	if err != nil {
		return nil, fmt.Errorf("rustwright: load %q: %w", path, err)
	}

	n := &nativeAPI{handle: h}
	defer func() {
		if recovered := recover(); recovered != nil {
			_ = purego.Dlclose(h)
			err = fmt.Errorf("rustwright: register C ABI: %v", recovered)
		}
	}()

	purego.RegisterLibFunc(&n.lastError, h, "rw_last_error")
	purego.RegisterLibFunc(&n.stringFree, h, "rw_string_free")
	purego.RegisterLibFunc(&n.bytesFree, h, "rw_bytes_free")
	purego.RegisterLibFunc(&n.decodeWire, h, "rw_decode_wire")
	purego.RegisterLibFunc(&n.chromiumExecutablePath, h, "rw_chromium_executable_path")
	purego.RegisterLibFunc(&n.chromiumLaunch, h, "rw_chromium_launch")
	purego.RegisterLibFunc(&n.browserNewPage, h, "rw_browser_new_page")
	purego.RegisterLibFunc(&n.browserClose, h, "rw_browser_close")
	purego.RegisterLibFunc(&n.browserWSEndpoint, h, "rw_browser_ws_endpoint")
	purego.RegisterLibFunc(&n.browserFree, h, "rw_browser_free")
	purego.RegisterLibFunc(&n.pageTargetID, h, "rw_page_target_id")
	purego.RegisterLibFunc(&n.pageGoto, h, "rw_page_goto")
	purego.RegisterLibFunc(&n.pageClick, h, "rw_page_click")
	purego.RegisterLibFunc(&n.pageFill, h, "rw_page_fill")
	purego.RegisterLibFunc(&n.pageTitle, h, "rw_page_title")
	purego.RegisterLibFunc(&n.pageTextContent, h, "rw_page_text_content")
	purego.RegisterLibFunc(&n.pageEvaluate, h, "rw_page_evaluate")
	purego.RegisterLibFunc(&n.pageScreenshot, h, "rw_page_screenshot")
	purego.RegisterLibFunc(&n.pageClose, h, "rw_page_close")
	purego.RegisterLibFunc(&n.pageFree, h, "rw_page_free")
	wireDecodeNative.Lock()
	wireDecodeNative.native = n
	wireDecodeNative.Unlock()
	return n, nil
}

func currentWireDecodeNative() (*nativeAPI, error) {
	wireDecodeNative.RLock()
	native := wireDecodeNative.native
	wireDecodeNative.RUnlock()
	if native == nil {
		return nil, errors.New("rustwright: no native library is loaded for wire decoding")
	}
	return native, nil
}

func (n *nativeAPI) decodeWireJSON(data []byte) ([]byte, error) {
	wireBuf, wirePtr, err := cString(string(data))
	if err != nil {
		return nil, err
	}
	var out uintptr
	err = n.onOSThread(func() int32 {
		return n.decodeWire(wirePtr, &out)
	})
	runtime.KeepAlive(wireBuf)
	if err != nil {
		return nil, fmt.Errorf("decode evaluate JSON: %w", err)
	}
	if out == 0 {
		return nil, errors.New("decode evaluate JSON: native call returned a null string")
	}
	decoded := []byte(copyCString(out))
	n.stringFree(out)
	return decoded, nil
}

// onOSThread keeps a fallible ABI call and its immediate rw_last_error lookup
// on one OS thread. The Rust error slot is thread-local and borrowed.
func (n *nativeAPI) onOSThread(call func() int32) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	status := call()
	if status == 0 {
		return nil
	}
	return n.copyLastError(status)
}

func (n *nativeAPI) copyLastError(status int32) error {
	ptr := n.lastError() // Must be the first ABI call after the failure.
	message := copyCString(ptr)
	if message == "" {
		message = "native call failed without an error message"
	}
	return fmt.Errorf("rustwright: %s (status %d)", message, status)
}

func (n *nativeAPI) directString(call func() uintptr, operation string) (string, error) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	ptr := call()
	if ptr == 0 {
		// Direct-return string functions document NULL as failure.
		err := n.copyLastError(-1)
		return "", fmt.Errorf("%s: %w", operation, err)
	}
	value := copyCString(ptr)
	n.stringFree(ptr)
	return value, nil
}

func cString(value string) ([]byte, *byte, error) {
	for i := 0; i < len(value); i++ {
		if value[i] == 0 {
			return nil, nil, errors.New("rustwright: strings passed to the C ABI cannot contain NUL")
		}
	}
	buf := make([]byte, len(value)+1)
	copy(buf, value)
	return buf, &buf[0], nil
}

func optionalCString(value string, present bool) ([]byte, *byte, error) {
	if !present {
		return nil, nil, nil
	}
	return cString(value)
}

func copyCString(ptr uintptr) string {
	if ptr == 0 {
		return ""
	}
	base := pointerFromUintptr(ptr)
	const maxInt = int(^uint(0) >> 1)
	length := 0
	for length < maxInt && *(*byte)(unsafe.Add(base, length)) != 0 {
		length++
	}
	if length == 0 {
		return ""
	}
	return string(unsafe.Slice((*byte)(base), length))
}

func pointerFromUintptr(ptr uintptr) unsafe.Pointer {
	// The address originates in native code. This indirection avoids uintptr
	// arithmetic while converting its representation for a bounded copy.
	return *(*unsafe.Pointer)(unsafe.Pointer(&ptr))
}
