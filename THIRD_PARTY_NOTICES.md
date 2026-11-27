# Source and runtime notices

H3 Creative Studio is distributed under the MIT licence in LICENSE. The following source components retain their original notices.

| Included files | Origin |
| --- | --- |
| vendor/prompt | Adapted graph transfer, MMH3 transfer, timing, audiovisual join, audio helpers, branch-local world rules and Film/Game instructions from [H3 Prompt Studio](https://github.com/BesianSherifaj-AI/h3-prompt-studio/tree/a6b2a91fe6f0dd448d9d65d79a285d4b5a08e4e5), MIT. See vendor/prompt/ORIGIN.md, LICENSE and THIRD_PARTY_NOTICES.md. |
| vendor/atelier/layers.py, vendor/image_graph | Layer compositor and Python graph recipes from [H3 Image Editor](https://github.com/BesianSherifaj-AI/h3-image-editor/tree/0eaf97b35071fa299a41fd9a585eda45fef4464b), MIT. Both directories include the upstream licence and notices. |
| Image workflow recipes | Derived from Comfy Org workflow templates where indicated upstream. The template licence is in vendor/image_graph/licenses/Comfy-Org-workflow-templates-LICENSE.txt. |

The notices retained from the original projects also describe files distributed by those projects. This source selection contains the Python modules and three prompt text files listed above. The Studio desktop interface is implemented here with HTML, CSS and JavaScript.

ComfyUI and installed custom nodes run as separate processes. LM Studio serves Director requests locally. UPSCALE runs in its own Python environment. FFmpeg and ffprobe are external executables. Each runtime, model, LoRA, codec and Python distribution retains its upstream licence and terms. The Windows ZIP includes the pinned Python runtime and its desktop dependencies. Their notices accompany the download. Model weights and the optional rendering engines are obtained separately.


The Barlow Semi Condensed font is distributed under the SIL Open Font License in [fonts/OFL.txt](h3studio/web/fonts/OFL.txt). The bundled Lucide icon paths use the licences in [icons-LICENSE.txt](h3studio/web/icons-LICENSE.txt). Tidal Relay contains original bundled example media. See the [starter credits](h3studio/starter/README.md). The SAM 3.1 and SeedVR2 still-image graph builders adapt Atelier’s `backend/helpers.py` under its included MIT licence.

The optional Visual Director uses the vision planning and review approach from H3 Image Editor. The reworked game controls use the world state validation and action rules from H3 Prompt Studio.
