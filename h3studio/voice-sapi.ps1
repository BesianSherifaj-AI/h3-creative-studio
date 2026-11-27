param([Parameter(Mandatory=$true)][string]$RequestFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = [IO.File]::ReadAllText($RequestFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
$directory = [IO.Path]::GetDirectoryName($RequestFile)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($request.mode -eq 'voices') {
        $rows = @($synth.GetInstalledVoices() | Where-Object Enabled | ForEach-Object {
            @{ name=$_.VoiceInfo.Name; language=$_.VoiceInfo.Culture.Name }
        })
        $json = ConvertTo-Json -InputObject $rows -Compress
        [IO.File]::WriteAllText((Join-Path $directory 'voices.json'), $json, [Text.Encoding]::UTF8)
    } else {
        $synth.SelectVoice([string]$request.voice)
        $synth.Rate = [int]$request.rate
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
            24000,
            [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono
        )
        for ($i = 0; $i -lt $request.segments.Count; $i++) {
            $synth.SetOutputToWaveFile((Join-Path $directory "$i.wav"), $format)
            $synth.Speak([string]$request.segments[$i])
            $synth.SetOutputToNull()
        }
    }
} finally {
    $synth.Dispose()
}
