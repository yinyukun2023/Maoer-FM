using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Run as x86: the Tolk DLLs supplied for this build cannot load in Python x64.
internal static class TolkBridge
{
    [DllImport("Tolk.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern void Tolk_Load();

    [DllImport("Tolk.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern void Tolk_Unload();

    [DllImport("Tolk.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern IntPtr Tolk_DetectScreenReader();

    [DllImport("Tolk.dll", CallingConvention = CallingConvention.Cdecl, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.I1)]
    private static extern bool Tolk_Output(
        [MarshalAs(UnmanagedType.LPWStr)] string text,
        [MarshalAs(UnmanagedType.I1)] bool interrupt
    );

    private static int Main()
    {
        bool loaded = false;
        try
        {
            Directory.SetCurrentDirectory(AppDomain.CurrentDomain.BaseDirectory);
            Console.InputEncoding = new UTF8Encoding(false);
            Console.OutputEncoding = new UTF8Encoding(false);
            // Some vendor DLLs print diagnostics to stdout; reserve stderr
            // for the protocol so those messages cannot corrupt responses.
            Tolk_Load();
            loaded = true;
            if (Tolk_DetectScreenReader() == IntPtr.Zero)
            {
                Console.Error.WriteLine("NONE");
                return 0;
            }

            Console.Error.WriteLine("READY");
            string line;
            while ((line = Console.ReadLine()) != null)
            {
                if (line == "QUIT")
                    break;
                try
                {
                    string text = Encoding.UTF8.GetString(Convert.FromBase64String(line));
                    // Timed captions should not build a long speech backlog.
                    Console.Error.WriteLine(Tolk_Output(text, true) ? "OK" : "FAIL");
                }
                catch (Exception)
                {
                    Console.Error.WriteLine("FAIL");
                }
            }
            return 0;
        }
        catch (Exception)
        {
            Console.Error.WriteLine("ERROR");
            return 1;
        }
        finally
        {
            if (loaded)
                Tolk_Unload();
        }
    }
}
