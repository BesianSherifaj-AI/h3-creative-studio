"""Fresh Windows installs retain a working private FFmpeg pair across restart."""
import hashlib
import io
import os
import zipfile

import pytest

from h3studio import community


@pytest.fixture
def release(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in ["bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/avutil.dll", "LICENSE.txt",
                     "../../elsewhere.txt", "bin/other.exe"]:
            archive.writestr("ffmpeg-build/" + name, name.encode())
    data=stream.getvalue()
    monkeypatch.setenv("PATH", str(tmp_path/"system-bin"))
    monkeypatch.setattr(community.platform, "system", lambda: "Windows")
    monkeypatch.setattr(community.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(community, "DOWNLOAD_BYTES", len(data))
    monkeypatch.setattr(community, "SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(community.urllib.request, "urlopen", lambda req,timeout: io.BytesIO(data))
    monkeypatch.setattr(community, "_verify_programs", lambda folder: None)
    return tmp_path/"studio-home"


def test_private_install_completes_a_partial_system_pair_and_repairs_missing_library(release, monkeypatch):
    monkeypatch.setattr(community.shutil,"which",lambda name,path: "/system/ffmpeg" if name=="ffmpeg" else None)
    assert community.status(release)["media"]["missing"]==["ffprobe"]
    progress=[]
    result=community.install(release, progress=lambda percent,message:progress.append(percent))["media"]
    assert result["source"]=="private" and result["ready"] and progress[-1]==100
    folder=community._private(community._root(release))
    assert {p.name for p in folder.iterdir()}=={"ffmpeg.exe","ffprobe.exe","avutil.dll","LICENSE.txt"}
    monkeypatch.setenv("PATH","")
    assert community.configure(release)["media"]==result and os.environ["PATH"]==str(folder)
    (folder/"avutil.dll").unlink()
    assert community.configure(release)["media"]["missing"]==["ffprobe"]
    assert str(folder) not in os.environ["PATH"]
    repaired=community.install(release)["media"]
    assert repaired["ready"] and repaired["ffmpeg"]!=result["ffmpeg"]


def test_checksum_cancel_and_validation_failure_preserve_preexisting_system(release, monkeypatch):
    monkeypatch.setattr(community.shutil,"which",lambda name,path: None)
    with pytest.raises(InterruptedError):
        community.install(release,cancel=lambda:True)
    with monkeypatch.context() as m:
        m.setattr(community, "SHA256", "0"*64)
        with pytest.raises(ValueError,match="checksum"):
            community.install(release)
    def bad_pair(path):
        raise ValueError("invalid pair")
    monkeypatch.setattr(community,"_verify_programs",bad_pair)
    with pytest.raises(ValueError,match="invalid pair"):
        community.install(release)
    assert not list(community._root(release).iterdir()) and not community.status(release)["media"]["ready"]


def test_already_available_uses_system_pair_without_network(release, monkeypatch):
    monkeypatch.setattr(community.shutil,"which",lambda name,path: "/system/"+name)
    def no_download(*args,**kwargs):
        pytest.fail("Unexpected download")
    monkeypatch.setattr(community.urllib.request,"urlopen",no_download)
    assert community.install(release)["media"]["source"]=="system"
    assert not community._root(release).exists()
