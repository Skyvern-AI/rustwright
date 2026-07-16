using System.Runtime.InteropServices;

namespace Rustwright;

public sealed class RustwrightException : Exception
{
    public RustwrightException(string message)
        : base(message)
    {
    }
}

internal static class CAbiString
{
    internal static void Validate(params string?[] values)
    {
        if (values.Any(value => value?.Contains('\0', StringComparison.Ordinal) == true))
        {
            throw new RustwrightException("strings passed to the C ABI cannot contain NUL");
        }
    }
}

internal static class NativeError
{
    internal static void ThrowIfFailed(int status)
    {
        if (status == 0)
        {
            return;
        }

        throw new RustwrightException(
            CopyLastError($"Rustwright native call failed with status {status}."));
    }

    internal static RustwrightException FromNullResult(string operation)
    {
        return new RustwrightException(CopyLastError($"{operation} returned a null pointer."));
    }

    internal static string CopyLastError(string fallback)
    {
        // This must be the first ABI call after the failed operation.
        var errorPointer = NativeMethods.LastError();
        return errorPointer == IntPtr.Zero
            ? fallback
            : Marshal.PtrToStringUTF8(errorPointer) ?? fallback;
    }

    internal static string ReadOwnedString(IntPtr pointer)
    {
        try
        {
            return Marshal.PtrToStringUTF8(pointer)
                ?? throw new RustwrightException("Rustwright returned invalid UTF-8 string data.");
        }
        finally
        {
            NativeMethods.StringFree(pointer);
        }
    }
}
