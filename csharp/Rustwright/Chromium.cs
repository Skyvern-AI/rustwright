using System.Text.Json;
using System.Text.Json.Nodes;
using System.Runtime.InteropServices;

namespace Rustwright;

public static class Chromium
{
    public static Browser Launch(LaunchOptions? options = null)
    {
        NativeLibraryLoader.EnsureConfigured();
        var optionsJson = JsonSerializer.Serialize(options ?? new LaunchOptions(), JsonWire.SerializerOptions);
        CAbiString.Validate(optionsJson);
        var status = NativeMethods.ChromiumLaunch(optionsJson, out var pointer);
        NativeError.ThrowIfFailed(status);
        if (pointer == IntPtr.Zero)
        {
            throw NativeError.FromNullResult("rw_chromium_launch");
        }

        return new Browser(new BrowserSafeHandle(pointer));
    }

    public static string? ExecutablePath()
    {
        NativeLibraryLoader.EnsureConfigured();
        var status = NativeMethods.ChromiumExecutablePath(out var pointer);
        NativeError.ThrowIfFailed(status);
        return pointer == IntPtr.Zero ? null : NativeError.ReadOwnedString(pointer);
    }
}

public sealed class Browser : IDisposable
{
    private readonly object gate = new();
    private readonly BrowserSafeHandle handle;
    private readonly HashSet<Page> pages = [];
    private bool closed;

    internal Browser(BrowserSafeHandle handle)
    {
        this.handle = handle;
    }

    ~Browser()
    {
        try
        {
            Close();
        }
        catch
        {
            // Finalizers cannot surface lifecycle errors.
        }
    }

    internal object Gate => gate;

    public Page NewPage()
    {
        lock (gate)
        {
            EnsureOpen();
            var status = NativeMethods.BrowserNewPage(handle, out var pointer);
            NativeError.ThrowIfFailed(status);
            if (pointer == IntPtr.Zero)
            {
                throw NativeError.FromNullResult("rw_browser_new_page");
            }

            var page = new Page(this, new PageSafeHandle(pointer));
            pages.Add(page);
            return page;
        }
    }

    public string WsEndpoint()
    {
        lock (gate)
        {
            EnsureOpen();
            var pointer = NativeMethods.BrowserWsEndpoint(handle);
            if (pointer == IntPtr.Zero)
            {
                throw NativeError.FromNullResult("rw_browser_ws_endpoint");
            }

            return NativeError.ReadOwnedString(pointer);
        }
    }

    public void Close()
    {
        lock (gate)
        {
            if (closed)
            {
                return;
            }

            closed = true;
            Exception? firstError = null;
            foreach (var page in pages.ToArray())
            {
                try
                {
                    page.Close();
                }
                catch (Exception error)
                {
                    firstError ??= error;
                }
            }

            if (handle.TryStartClose())
            {
                try
                {
                    NativeError.ThrowIfFailed(NativeMethods.BrowserClose(handle));
                }
                catch (Exception error)
                {
                    firstError ??= error;
                }
                finally
                {
                    handle.Dispose();
                }
            }

            GC.SuppressFinalize(this);

            if (firstError is not null)
            {
                throw firstError;
            }
        }
    }

    public void Dispose() => Close();

    internal void Remove(Page page)
    {
        lock (gate)
        {
            pages.Remove(page);
        }
    }

    private void EnsureOpen()
    {
        if (closed || handle.IsClosed || handle.IsInvalid)
        {
            throw new ObjectDisposedException(nameof(Browser));
        }
    }
}

public sealed class Page : IDisposable
{
    private readonly Browser owner;
    private readonly PageSafeHandle handle;
    private bool closed;

    internal Page(Browser owner, PageSafeHandle handle)
    {
        this.owner = owner;
        this.handle = handle;
    }

    public string TargetId()
    {
        lock (owner.Gate)
        {
            EnsureOpen();
            var pointer = NativeMethods.PageTargetId(handle);
            if (pointer == IntPtr.Zero)
            {
                throw NativeError.FromNullResult("rw_page_target_id");
            }

            return NativeError.ReadOwnedString(pointer);
        }
    }

    public object? Goto(string url, GotoOptions? options = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(url);
        CAbiString.Validate(url, options?.WaitUntil, options?.Referer);
        lock (owner.Gate)
        {
            EnsureOpen();
            var status = NativeMethods.PageGoto(
                handle,
                url,
                options?.WaitUntil,
                Timeout(options?.Timeout),
                options?.Referer,
                out var pointer);
            NativeError.ThrowIfFailed(status);
            if (pointer == IntPtr.Zero)
            {
                return null;
            }

            var json = NativeError.ReadOwnedString(pointer);
            return JsonNode.Parse(json);
        }
    }

    public void Click(string selector, double? timeoutMs = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(selector);
        CAbiString.Validate(selector);
        lock (owner.Gate)
        {
            EnsureOpen();
            NativeError.ThrowIfFailed(
                NativeMethods.PageClick(handle, selector, Timeout(timeoutMs)));
        }
    }

    public void Fill(string selector, string value, double? timeoutMs = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(selector);
        ArgumentNullException.ThrowIfNull(value);
        CAbiString.Validate(selector, value);
        lock (owner.Gate)
        {
            EnsureOpen();
            NativeError.ThrowIfFailed(
                NativeMethods.PageFill(handle, selector, value, Timeout(timeoutMs)));
        }
    }

    public string Title(double? timeoutMs = null)
    {
        lock (owner.Gate)
        {
            EnsureOpen();
            var status = NativeMethods.PageTitle(handle, Timeout(timeoutMs), out var pointer);
            NativeError.ThrowIfFailed(status);
            if (pointer == IntPtr.Zero)
            {
                throw NativeError.FromNullResult("rw_page_title");
            }

            return NativeError.ReadOwnedString(pointer);
        }
    }

    public string? TextContent(string selector, double? timeoutMs = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(selector);
        CAbiString.Validate(selector);
        lock (owner.Gate)
        {
            EnsureOpen();
            var status = NativeMethods.PageTextContent(
                handle,
                selector,
                Timeout(timeoutMs),
                out var pointer);
            NativeError.ThrowIfFailed(status);
            return pointer == IntPtr.Zero ? null : NativeError.ReadOwnedString(pointer);
        }
    }

    public object? Evaluate(string expression) => EvaluateCore(expression, null, null);

    public object? Evaluate(string expression, object? argument, double? timeoutMs = null) =>
        EvaluateCore(expression, JsonSerializer.Serialize(argument, JsonWire.SerializerOptions), timeoutMs);

    public object? EvaluateWithTimeout(string expression, double timeoutMs) =>
        EvaluateCore(expression, null, timeoutMs);

    public byte[] Screenshot(ScreenshotOptions? options = null)
    {
        lock (owner.Gate)
        {
            EnsureOpen();
            var optionsJson = options is null
                ? null
                : JsonSerializer.Serialize(options, JsonWire.SerializerOptions);
            CAbiString.Validate(optionsJson);
            var status = NativeMethods.PageScreenshot(
                handle,
                optionsJson,
                out var pointer,
                out var length);
            NativeError.ThrowIfFailed(status);

            try
            {
                if (length > int.MaxValue)
                {
                    throw new RustwrightException($"Screenshot is too large for a managed byte array ({length} bytes).");
                }

                if (pointer == IntPtr.Zero)
                {
                    if (length != 0)
                    {
                        throw new RustwrightException("Screenshot returned a null buffer with a nonzero length.");
                    }

                    return [];
                }

                var bytes = new byte[(int)length];
                Marshal.Copy(pointer, bytes, 0, bytes.Length);
                return bytes;
            }
            finally
            {
                NativeMethods.BytesFree(pointer, length);
            }
        }
    }

    public void Close(double? timeoutMs = null, bool runBeforeUnload = false)
    {
        lock (owner.Gate)
        {
            if (closed)
            {
                return;
            }

            closed = true;
            try
            {
                if (handle.TryStartClose())
                {
                    NativeError.ThrowIfFailed(
                        NativeMethods.PageClose(
                            handle,
                            Timeout(timeoutMs),
                            runBeforeUnload ? 1 : 0));
                }
            }
            finally
            {
                handle.Dispose();
                owner.Remove(this);
            }
        }
    }

    public void Dispose() => Close();

    private object? EvaluateCore(string expression, string? argumentJson, double? timeoutMs)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(expression);
        CAbiString.Validate(expression, argumentJson);
        lock (owner.Gate)
        {
            EnsureOpen();
            var status = NativeMethods.PageEvaluate(
                handle,
                expression,
                argumentJson,
                Timeout(timeoutMs),
                out var pointer);
            NativeError.ThrowIfFailed(status);
            if (pointer == IntPtr.Zero)
            {
                throw NativeError.FromNullResult("rw_page_evaluate");
            }

            return JsonWire.Decode(NativeError.ReadOwnedString(pointer));
        }
    }

    private static double Timeout(double? milliseconds) => milliseconds ?? double.NaN;

    private void EnsureOpen()
    {
        if (closed || handle.IsClosed || handle.IsInvalid)
        {
            throw new ObjectDisposedException(nameof(Page));
        }
    }
}
