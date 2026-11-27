using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("H3 Creative Studio")]
[assembly: AssemblyProduct("H3 Creative Studio")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]
[assembly: AssemblyInformationalVersion("2.0.0")]

internal static class Launcher
{
    private const string Title = "H3 Creative Studio";
    private const string WebView2Key = @"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    // Match CommandLineToArgvW, including backslashes before quotes and the closing quote.
    private static string Quote(string value)
    {
        var output = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { output.Append('\\', slashes * 2 + 1); }
            else { output.Append('\\', slashes); }
            output.Append(ch);
            slashes = 0;
        }
        return output.Append('\\', slashes * 2).Append('"').ToString();
    }

    private static bool HasWebView2()
    {
        string fixedRuntime = Environment.GetEnvironmentVariable("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER");
        if (!String.IsNullOrEmpty(fixedRuntime) && File.Exists(Path.Combine(fixedRuntime, "msedgewebview2.exe"))) return true;
        foreach (RegistryHive hive in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
        foreach (RegistryView view in new[] { RegistryView.Registry32, RegistryView.Registry64 })
        using (var root = RegistryKey.OpenBaseKey(hive, view))
        using (var key = root.OpenSubKey(WebView2Key))
        {
            string value = key == null ? "" : Convert.ToString(key.GetValue("pv"));
            if (!String.IsNullOrEmpty(value) && value != "0.0.0.0") return true;
        }
        return false;
    }

    private static void CreateShortcut(string folder)
    {
        string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Title + " (Portable).lnk");
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell", true));
        dynamic link = shell.CreateShortcut(path);
        string exe = Assembly.GetExecutingAssembly().Location;
        if (File.Exists(path) && !String.Equals(link.TargetPath, exe, StringComparison.OrdinalIgnoreCase))
        {
            if (MessageBox.Show("Update the portable desktop shortcut to this copy?", Title,
                                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
        }
        link.TargetPath = exe;
        link.WorkingDirectory = folder;
        link.IconLocation = exe + ",0";
        link.Description = Title;
        link.Save();
        MessageBox.Show("Desktop shortcut created.", Title, MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string folder = AppDomain.CurrentDomain.BaseDirectory;
            if (args.Length == 1 && args[0] == "--create-shortcut") { CreateShortcut(folder); return 0; }
            if (Array.IndexOf(args, "--serve") < 0 && !HasWebView2())
            {
                if (MessageBox.Show("This desktop window uses Microsoft Edge WebView2. Open Microsoft's Evergreen Runtime download page? After installation, launch Studio again.", Title,
                                    MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                    Process.Start("https://developer.microsoft.com/microsoft-edge/webview2/#download");
                return 2;
            }
            string python = Path.Combine(folder, "runtime", "pythonw.exe");
            string script = Path.Combine(folder, "app", "studio.py");
            if (!File.Exists(python) || !File.Exists(script)) throw new FileNotFoundException("Extract the entire ZIP before opening H3 Creative Studio.");
            var arguments = new List<string> { "-E", "-s", "-B", Quote(script) };
            foreach (string arg in args) arguments.Add(Quote(arg));
            var info = new ProcessStartInfo(python, String.Join(" ", arguments));
            // Preserve caller-relative --home or --project paths. Python imports from the absolute script directory.
            info.WorkingDirectory = Environment.CurrentDirectory;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables["H3_PORTABLE_ROOT"] = folder.TrimEnd(Path.DirectorySeparatorChar);
            using (var worker = Process.Start(info))
            {
                worker.WaitForExit();
                return worker.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
