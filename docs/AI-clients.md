# Connect an AI client

Open the **AI clients** example from Studio's example library to try the connection in a separate copy of Tidal Relay. For your own film, open its project and choose **Setup → Connect an AI client**.

1. Choose **Read project** to explore, or **Read project and propose drafts** to collaborate. Choose **Enable connection**.
2. Choose **Copy configuration**. In your assistant's MCP settings, import the entry or add a **stdio** server with the displayed `command` and ordered `args`. Clients using a different configuration format can use those same values. The Windows download includes the Python interpreter and adapter.
3. Enable or refresh the server in your client and ask it *“What can the relay operator see and carry? List the available choices at this turn.”*
4. Choose **Refresh inbox** in Studio to see new proposals. Review a storyboard in Film, a turn in Game, or a prompt and frame guide in the connection dialog. **Use in Voice** opens a proposed script for casting, editing and audition. Accepted scripts can be reopened from the connection dialog.

Each connection belongs to the open project and window. It lasts up to eight hours and ends on **Disconnect client**, a project switch or app exit. To reconnect, enable a new connection and replace the client's configuration. The copied command uses the absolute paths from the current installation and the current session file. Keep the session file in the local user's application data. The client runs the adapter as a subprocess and Studio listens on a private loopback port. An assistant using a hosted model may send its tool results to its provider according to that client's settings.

## Try a production workflow

**Game** — Ask *“Read the active Game world. Suggest one authored choice that advances the relay mystery. Wait for my approval. Then read the new turn and draft a short line for the shore keeper.”* The client reads `game_world`, proposes a choice by its returned ID and checks `proposal_status`. Choose **Play choice** to activate that branch. For an original branch, `propose_game_turn` takes the parent ID, world revision, action, scene and follow-up choices. It can use a grounded control returned by the world, such as picking up the field lamp. Accepting the turn applies the Game engine's inventory and movement rules.

**Image and Motion** — Ask *“Find my saved image revisions and the second shot. Propose a revised camera prompt with the receiver plate at its middle frame.”* Guide positions use the 24 fps delivered cut starting at frame zero. The proposal identifies the exact Image revision and target frame. Approval updates the editable draft and places the timed image guide, preserving independently placed audio and video guides. When the shot changes before approval, Studio asks for a fresh proposal.

**Film and Edit** — Ask *“Read my cut and propose a two-shot ending. Give each shot a title, duration and visual action.”* The storyboard appears in Film for review. Approval adds editable rows to the project. Choose which shots to render in Studio. `project_overview` returns the ordered cut, selected take IDs and durations. `shot_inspect` returns the working prompt, guides, takes and continuity flags.

**Voice** — Ask *“Write a twelve-word line for the keeper in this Game scene. Send it to Voice as a script.”* Choose **Use in Voice**, select an installed voice or configured local speech server, and generate the clip. Place it in the delivery mix when ready.

**A new Game** — Ask *“Open an independent story about a lighthouse keeper on a floating island.”* The assistant can provide a player name, opening location and optional appearance in `setup` to propose a new opening. Approval creates its first playable turn. For a continuation, send its parent turn ID and current world revision instead.

## Client contract

The default overview and individual shot inspection both report `start_frame` (inclusive), `end_frame` (exclusive), `source_in` and `source_out` (seconds), and `duration` in delivered seconds. `nominal_duration` records the shot's authored length. Shots outside the active cut have null timeline positions. A clip's `media_ready` flag tells the client whether Studio could inspect its media. With missing media or media tools the Edit room uses the shot's planned duration. Call `project_overview` with `{"view":"timeline"}` for output track routing and saved narration.

Studio supports the MCP stdio initialization handshake, ping, tool discovery and tool calls. The adapter exchanges newline-delimited UTF-8 JSON-RPC messages. Tool results contain JSON text and, on supported protocol versions, `structuredContent`. Execution errors use `isError`. Each connection supports 240 requests per minute. Every tool publishes its JSON input schema through `tools/list`. See the [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) and [initialization lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).

- **Inspect** with `project_overview`, `image_library`, `shot_inspect`, `film_boards`, `game_branches`, `game_world` and `proposal_status`. For list tools, use `limit` and follow `next_offset` with `offset`.
- **Propose** with `propose_film`, `propose_game_choice`, `propose_game_turn`, `propose_shot_draft` and `propose_voice_script`. Studio advertises these five tools when the author enables proposals. Each takes a caller-chosen `request_id`. Retry the same input with the same key to recover its existing proposal.
- **Review** using the project inbox. The client can check pending, approved or dismissed status after reconnecting to the same project. Approval, rendering, audio generation, export and media import use the regular desktop controls.

For example, once a client has completed initialization, this call reads the active turn. Use the returned stable IDs for subsequent calls.

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"game_world","arguments":{}}}
```

If discovery reports an expired pairing, reopen **AI clients** and replace the client configuration. When the client only sees inspection tools, disconnect and pair again with **Read project and propose drafts**. When a proposed turn references a stale world, read its parent again and submit the corrected draft with a new request ID.
