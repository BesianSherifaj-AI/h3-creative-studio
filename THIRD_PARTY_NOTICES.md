# Source and runtime notices

H3 Creative Studio is distributed under the MIT licence in LICENSE. The following source components retain their original notices.

| Included files | Origin |
| --- | --- |
| vendor/prompt | Adapted graph transfer, MMH3 transfer, timing, audiovisual join, audio helpers and Film/Game instructions from [H3 Prompt Studio](https://github.com/BesianSherifaj-AI/h3-prompt-studio/tree/a6b2a91fe6f0dd448d9d65d79a285d4b5a08e4e5), MIT. See vendor/prompt/ORIGIN.md, LICENSE and THIRD_PARTY_NOTICES.md. |
| vendor/atelier/layers.py, vendor/image_graph | Layer compositor and Python graph recipes from [H3 Image Editor](https://github.com/BesianSherifaj-AI/h3-image-editor/tree/0eaf97b35071fa299a41fd9a585eda45fef4464b), MIT. Both directories include the upstream licence and notices. |
| Image workflow recipes | Derived from Comfy Org workflow templates where indicated upstream. The template licence is in vendor/image_graph/licenses/Comfy-Org-workflow-templates-LICENSE.txt. |

The notices retained from the original projects also describe files distributed by those projects. This source selection contains the Python modules and three prompt text files listed above. The Studio desktop interface is implemented here with HTML, CSS and JavaScript.

ComfyUI and installed custom nodes run as separate processes. LM Studio serves Director requests locally. UPSCALE runs in its own Python environment. FFmpeg and ffprobe are external executables. Each runtime, model, LoRA, codec and Python distribution retains its upstream licence and terms. Model weights, generated films, legacy project contents and runtime binaries are installed or created separately.
