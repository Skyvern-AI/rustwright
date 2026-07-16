using System.Text;

namespace Rustwright;

public static class DataUrls
{
    private const string Prefix = "data:text/html;charset=utf-8,";

    public static string FromHtml(string html)
    {
        ArgumentNullException.ThrowIfNull(html);
        var bytes = Encoding.UTF8.GetBytes(html);
        var result = new StringBuilder(Prefix.Length + bytes.Length * 3);
        result.Append(Prefix);

        foreach (var value in bytes)
        {
            if ((value >= (byte)'A' && value <= (byte)'Z') ||
                (value >= (byte)'a' && value <= (byte)'z') ||
                (value >= (byte)'0' && value <= (byte)'9') ||
                value is (byte)'-' or (byte)'.' or (byte)'_' or (byte)'~')
            {
                result.Append((char)value);
            }
            else
            {
                result.Append('%');
                result.Append(value.ToString("X2", System.Globalization.CultureInfo.InvariantCulture));
            }
        }

        return result.ToString();
    }
}
