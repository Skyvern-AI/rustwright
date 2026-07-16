using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Rustwright;

internal static class NativeMethods
{
    internal const string LibraryName = "rustwright_capi";

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_last_error")]
    internal static extern IntPtr LastError();

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_string_free")]
    internal static extern void StringFree(IntPtr value);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_bytes_free")]
    internal static extern void BytesFree(IntPtr buffer, nuint length);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_chromium_executable_path")]
    internal static extern int ChromiumExecutablePath(out IntPtr path);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_chromium_launch")]
    internal static extern int ChromiumLaunch(
        [MarshalAs(UnmanagedType.LPUTF8Str)] string optionsJson,
        out IntPtr browser);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_browser_new_page")]
    internal static extern int BrowserNewPage(BrowserSafeHandle browser, out IntPtr page);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_browser_close")]
    internal static extern int BrowserClose(BrowserSafeHandle browser);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_browser_close")]
    internal static extern int BrowserCloseRaw(IntPtr browser);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_browser_ws_endpoint")]
    internal static extern IntPtr BrowserWsEndpoint(BrowserSafeHandle browser);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_browser_free")]
    internal static extern void BrowserFree(IntPtr browser);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_target_id")]
    internal static extern IntPtr PageTargetId(PageSafeHandle page);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_goto")]
    internal static extern int PageGoto(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string url,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string? waitUntil,
        double timeoutMsOrNan,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string? referer,
        out IntPtr responseJson);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_click")]
    internal static extern int PageClick(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string selector,
        double timeoutMsOrNan);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_fill")]
    internal static extern int PageFill(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string selector,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string value,
        double timeoutMsOrNan);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_title")]
    internal static extern int PageTitle(PageSafeHandle page, double timeoutMsOrNan, out IntPtr title);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_text_content")]
    internal static extern int PageTextContent(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string selector,
        double timeoutMsOrNan,
        out IntPtr text);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_evaluate")]
    internal static extern int PageEvaluate(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string expression,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string? argumentJson,
        double timeoutMsOrNan,
        out IntPtr resultJson);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_screenshot")]
    internal static extern int PageScreenshot(
        PageSafeHandle page,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string? optionsJson,
        out IntPtr buffer,
        out nuint length);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_close")]
    internal static extern int PageClose(PageSafeHandle page, double timeoutMsOrNan, int runBeforeUnload);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_close")]
    internal static extern int PageCloseRaw(IntPtr page, double timeoutMsOrNan, int runBeforeUnload);

    [DllImport(LibraryName, CallingConvention = CallingConvention.Cdecl, EntryPoint = "rw_page_free")]
    internal static extern void PageFree(IntPtr page);
}

public static class NativeLibraryLoader
{
    private static readonly object Gate = new();
    private static string? configuredPath;
    private static IntPtr libraryHandle;
    private static bool resolverInstalled;

    public static string Configure(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        var fullPath = Path.GetFullPath(path);
        if (!File.Exists(fullPath))
        {
            throw new FileNotFoundException("Rustwright native library was not found.", fullPath);
        }

        lock (Gate)
        {
            if (configuredPath is not null && !PathEquals(configuredPath, fullPath))
            {
                throw new InvalidOperationException(
                    $"Rustwright is already configured with native library '{configuredPath}'.");
            }

            configuredPath = fullPath;
            InstallResolver();
        }

        return fullPath;
    }

    internal static void EnsureConfigured()
    {
        lock (Gate)
        {
            InstallResolver();
        }
    }

    private static IntPtr ResolveLibrary(
        string libraryName,
        Assembly assembly,
        DllImportSearchPath? searchPath)
    {
        if (!string.Equals(libraryName, NativeMethods.LibraryName, StringComparison.Ordinal))
        {
            return IntPtr.Zero;
        }

        lock (Gate)
        {
            if (libraryHandle != IntPtr.Zero)
            {
                return libraryHandle;
            }

            // An explicit path is a strict pin: never substitute another library.
            if (configuredPath is not null)
            {
                libraryHandle = NativeLibrary.Load(configuredPath);
                return libraryHandle;
            }

            // Let .NET resolve the package's RID-specific runtimes/<rid>/native asset.
            if (NativeLibrary.TryLoad(libraryName, assembly, searchPath, out libraryHandle))
            {
                return libraryHandle;
            }

            // Source checkouts retain the existing target/release development fallback.
            var repositoryLibrary = Path.Combine(
                Environment.CurrentDirectory,
                "target",
                "release",
                DefaultLibraryFileName());
            return NativeLibrary.TryLoad(repositoryLibrary, out libraryHandle)
                ? libraryHandle
                : IntPtr.Zero;
        }
    }

    private static void InstallResolver()
    {
        if (resolverInstalled)
        {
            return;
        }

        NativeLibrary.SetDllImportResolver(typeof(NativeMethods).Assembly, ResolveLibrary);
        resolverInstalled = true;
    }

    private static string DefaultLibraryFileName()
    {
        if (OperatingSystem.IsMacOS())
        {
            return "librustwright_capi.dylib";
        }

        if (OperatingSystem.IsWindows())
        {
            return "rustwright_capi.dll";
        }

        return "librustwright_capi.so";
    }

    private static bool PathEquals(string left, string right) =>
        string.Equals(
            left,
            right,
            OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);
}

internal sealed class BrowserSafeHandle : SafeHandleZeroOrMinusOneIsInvalid
{
    private int closeStarted;

    internal BrowserSafeHandle(IntPtr handle)
        : base(true)
    {
        SetHandle(handle);
    }

    internal bool TryStartClose() => Interlocked.Exchange(ref closeStarted, 1) == 0;

    protected override bool ReleaseHandle()
    {
        if (Interlocked.Exchange(ref closeStarted, 1) == 0)
        {
            var status = NativeMethods.BrowserCloseRaw(handle);
            if (status != 0)
            {
                _ = NativeError.CopyLastError($"Rustwright native call failed with status {status}.");
            }
        }

        NativeMethods.BrowserFree(handle);
        return true;
    }
}

internal sealed class PageSafeHandle : SafeHandleZeroOrMinusOneIsInvalid
{
    private int closeStarted;

    internal PageSafeHandle(IntPtr handle)
        : base(true)
    {
        SetHandle(handle);
    }

    internal bool TryStartClose() => Interlocked.Exchange(ref closeStarted, 1) == 0;

    protected override bool ReleaseHandle()
    {
        if (Interlocked.Exchange(ref closeStarted, 1) == 0)
        {
            var status = NativeMethods.PageCloseRaw(handle, double.NaN, 0);
            if (status != 0)
            {
                _ = NativeError.CopyLastError($"Rustwright native call failed with status {status}.");
            }
        }

        NativeMethods.PageFree(handle);
        return true;
    }
}
