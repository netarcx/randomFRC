# randomFRC

24/7 RTMP stream that plays random FRC match videos from [The Blue Alliance](https://www.thebluealliance.com/), piped through yt-dlp + ffmpeg to a [Restreamer](https://datarhei.github.io/restreamer/) instance (which forwards to Twitch or any RTMP destination).

## Quick Start

### 1. Get a TBA API Key

Go to [thebluealliance.com/account](https://www.thebluealliance.com/account) and generate a Read API Key.

### 2. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `.env`:
```
TBA_API_KEY=your_tba_api_key_here
RTMP_TOKEN=your_restreamer_token_here
```

Edit `config.yaml` to set your filters (years, teams, states, etc.). The defaults will stream random matches from 2023-2024.

### 3. Start

```bash
docker compose up --build
```

This starts:
- **Restreamer** on `http://localhost:8080` (web UI) and `rtmp://localhost:1935` (RTMP ingest)
- **Streamer** which picks random FRC videos and streams them to Restreamer

### 4. Forward to Twitch

Open the Restreamer UI at `http://localhost:8080`, configure it to forward the incoming stream to your Twitch RTMP endpoint.

### 5. Verify

```bash
docker compose logs -f streamer
```

You should see log lines like:
```
Streaming: 2024casf_qm42 (dQw4w9WgXcQ)
Stream completed successfully: 2024casf_qm42
```

## GPU Acceleration

For NVIDIA GPU encoding:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

Requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). The streamer auto-detects available encoders (NVENC, VideoToolbox, VAAPI) or falls back to software x264.

Set `stream.hw_accel` in `config.yaml` to force a specific encoder: `auto`, `nvenc`, `videotoolbox`, `vaapi`, or `none`.

## Filter Examples

Stream only finals from California events:
```yaml
filters:
  years: [2024]
  states: ["CA"]
  comp_levels: ["f"]
```

Stream only matches involving specific teams:
```yaml
filters:
  years: [2023, 2024]
  teams: [254, 1678]
```

Stream from a specific event:
```yaml
filters:
  events: ["2024casf"]
```
