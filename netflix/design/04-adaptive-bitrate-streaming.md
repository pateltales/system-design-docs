# Deep Dive: Adaptive Bitrate Streaming

How video gets from S3 to the user's screen. At its core, video streaming is just
HTTP file serving with smart client-side logic -- but the "smart" part is where all
the complexity lives.

---

## 1. Segment-Based Streaming

Video is not streamed as a single continuous file. Instead, it is split into small,
independently decodable chunks called **segments**.

```
Full Movie (2 hours)
|
v
+----------+----------+----------+----------+-----+----------+
| Seg 0    | Seg 1    | Seg 2    | Seg 3    | ... | Seg 2699 |
| 0-4s     | 4-8s     | 8-12s   | 12-16s   |     | ~2:59:56 |
+----------+----------+----------+----------+-----+----------+
     |
     v
  Each segment exists at MULTIPLE quality levels:

  +------------------+  +------------------+  +------------------+
  | Seg 0 @ 235 kbps |  | Seg 0 @ 1050kbps |  | Seg 0 @ 4300kbps |
  | 240p             |  | 480p             |  | 1080p            |
  | ~117 KB          |  | ~525 KB          |  | ~2.1 MB          |
  +------------------+  +------------------+  +------------------+
```

### Key Properties of Each Segment

- **Duration**: 2-4 seconds (Netflix uses ~4s; shorter segments = faster adaptation
  but more HTTP overhead)
- **Starts with a keyframe (IDR frame)**: Every segment begins with a full image frame,
  not a delta. This makes each segment independently decodable -- you never need a
  previous segment to render the current one.
- **Self-contained metadata**: Each segment carries its own timing and decoding info
  inside the container format (fMP4).

### Why Segments?

| Property | Benefit |
|---|---|
| Independent decoding | Can switch quality between any two segments |
| Small file size | Fast to download, easy to cache on CDN edge nodes |
| HTTP-friendly | Each segment is a standard HTTP GET request -- works with existing CDN infrastructure |
| Seek-friendly | Seeking jumps to the nearest segment boundary, not byte offset |

---

## 2. Manifest Files

Before the player downloads any video, it fetches a **manifest file** that describes
what is available: which quality levels exist, where each segment lives, timing info,
and codec parameters.

### DASH (MPEG-DASH) -- Netflix's Primary Protocol

DASH uses an XML file called an **MPD (Media Presentation Description)**.

```xml
<!-- Simplified MPD example -->
<MPD type="static" mediaPresentationDuration="PT1H42M">

  <!-- Video Adaptation Set -->
  <AdaptationSet mimeType="video/mp4" codecs="avc1.4d401f">

    <Representation id="1" bandwidth="235000" width="320" height="240">
      <SegmentTemplate media="v_235/$Number$.m4s"
                       initialization="v_235/init.mp4"
                       duration="4000" timescale="1000"/>
    </Representation>

    <Representation id="2" bandwidth="1050000" width="720" height="480">
      <SegmentTemplate media="v_1050/$Number$.m4s"
                       initialization="v_1050/init.mp4"
                       duration="4000" timescale="1000"/>
    </Representation>

    <Representation id="3" bandwidth="4300000" width="1920" height="1080">
      <SegmentTemplate media="v_4300/$Number$.m4s"
                       initialization="v_4300/init.mp4"
                       duration="4000" timescale="1000"/>
    </Representation>

  </AdaptationSet>

  <!-- Audio Adaptation Set -->
  <AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2">
    <Representation id="a1" bandwidth="128000">
      <SegmentTemplate media="a_128/$Number$.m4s"
                       initialization="a_128/init.mp4"
                       duration="4000" timescale="1000"/>
    </Representation>
  </AdaptationSet>

</MPD>
```

Key fields in the MPD:
- **AdaptationSet**: Groups representations of the same content type (video, audio, subtitles)
- **Representation**: One quality level. `bandwidth` = required network throughput in bps
- **SegmentTemplate**: URL pattern for segments. `$Number$` is replaced with 0, 1, 2...
- **initialization**: One-time init segment with codec config (SPS/PPS for H.264)

### HLS (HTTP Live Streaming) -- Required for Apple Ecosystem

Apple mandates HLS for iOS and Safari. Uses M3U8 playlists (plain text, not XML).

```
#EXTM3U

# Master playlist -- lists available quality levels
#EXT-X-STREAM-INF:BANDWIDTH=235000,RESOLUTION=320x240
quality_240p/playlist.m3u8

#EXT-X-STREAM-INF:BANDWIDTH=1050000,RESOLUTION=720x480
quality_480p/playlist.m3u8

#EXT-X-STREAM-INF:BANDWIDTH=4300000,RESOLUTION=1920x1080
quality_1080p/playlist.m3u8
```

```
# Media playlist for 1080p -- lists actual segments
#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0

#EXTINF:4.000,
segment_0.m4s
#EXTINF:4.000,
segment_1.m4s
#EXTINF:4.000,
segment_2.m4s
...
```

### DASH vs HLS Comparison

| Aspect | DASH (MPEG-DASH) | HLS |
|---|---|---|
| Format | XML (MPD) | Plain text (M3U8) |
| Origin | MPEG consortium (open standard) | Apple (proprietary, widely adopted) |
| Netflix usage | Primary protocol for most devices | iOS, Safari, Apple TV |
| Container | fMP4 | Originally MPEG-TS, now also fMP4 |
| DRM | Widevine, PlayReady via CENC | FairPlay |
| Latency | Supports low-latency profiles | LHLS for low latency |
| Adoption | Android, Chrome, smart TVs, consoles | iOS, Safari, Apple TV |

### Key Insight

Neither DASH nor HLS involves a custom streaming protocol. The player simply makes
standard **HTTP GET requests** for segments. The CDN serves them like any other static
file. All the intelligence is on the client side -- the ABR algorithm decides which
quality to request next.

```
Player                         CDN Edge
  |                               |
  |-- GET /manifest.mpd --------->|
  |<-- 200 OK (MPD XML) ---------|
  |                               |
  |-- GET /v_1050/init.mp4 ----->|   (one-time init segment)
  |<-- 200 OK -------------------|
  |                               |
  |-- GET /v_1050/0.m4s -------->|   (segment 0 at 1050 kbps)
  |<-- 200 OK -------------------|
  |                               |
  |-- GET /v_4300/1.m4s -------->|   (segment 1 at 4300 kbps -- upgraded!)
  |<-- 200 OK -------------------|
  |                               |
  |-- GET /v_1050/2.m4s -------->|   (segment 2 back to 1050 -- bandwidth dropped)
  |<-- 200 OK -------------------|
```

---

## 3. ABR (Adaptive Bitrate) Algorithms

The ABR algorithm is the brain of the player. Before downloading each segment, it
decides which quality level to request. This decision happens entirely on the client.

### 3.1 Throughput-Based ABR

The simplest approach: measure how fast the last few segments downloaded, and pick
the highest bitrate that fits within that measured throughput.

```
Algorithm: Throughput-Based

  measured_throughput = bytes_downloaded / download_time
  safety_margin = 0.7   (use only 70% of measured bandwidth)

  for each representation (highest to lowest bitrate):
      if representation.bandwidth < measured_throughput * safety_margin:
          return representation

  return lowest_quality   (fallback)
```

**Problem**: Network throughput is noisy. A single slow segment (CDN hiccup, TCP
congestion window reset, shared WiFi) causes the algorithm to drop quality. Then the
next segment downloads fast (lower bitrate = faster download), so it jumps back up.
This creates **oscillation** -- quality ping-pongs between levels, which is visually
jarring.

```
Throughput-Based Oscillation Problem:

Quality   ^
  1080p   |          *         *         *
   720p   |        *   *     *   *     *   *
   480p   |      *       * *       * *       *
   240p   |    *
          +-----------------------------------------> Time
              Unstable -- annoying for the viewer
```

### 3.2 Buffer-Based ABR (BBA) -- Netflix's Approach

Netflix's key innovation. Instead of measuring throughput (which is noisy), make
decisions based on **how full the playback buffer is**. The buffer is an honest signal
of the player's health.

```
Algorithm: Buffer-Based (BBA)

  buffer_level = current buffer occupancy in seconds

  if buffer_level < RESERVOIR (e.g., 10s):
      return lowest_quality        -- emergency: prevent rebuffer at all costs

  else if buffer_level > UPPER (e.g., 50s):
      return highest_quality       -- buffer is healthy, go for max quality

  else:
      -- Linear interpolation between reservoir and upper threshold
      fraction = (buffer_level - RESERVOIR) / (UPPER - RESERVOIR)
      target_bitrate = min_bitrate + fraction * (max_bitrate - min_bitrate)
      return representation closest to target_bitrate
```

**Visual: BBA Mapping Function**

```
Selected     ^
Bitrate      |
             |                              +-----------  max bitrate
  4300 kbps  |                           __/
             |                        __/
  1050 kbps  |                     __/
             |                  __/
   235 kbps  |  +--------------/
             |  |
             +--+--------+-------------------+----------> Buffer Level
                0     RESERVOIR           UPPER
                       (10s)              (50s)

             |  Emergency |  Ramp-up zone  |  Cruise  |
```

**Why BBA works better**:

| Factor | Throughput-Based | Buffer-Based (BBA) |
|---|---|---|
| Signal quality | Noisy (TCP measurements) | Smooth (buffer changes slowly) |
| Oscillation | Frequent | Rare |
| Rebuffer rate | Higher | 10-20% lower |
| Reaction to congestion | Immediate (may overreact) | Gradual (buffer absorbs spikes) |
| Reaction to sustained drop | Fast | Slower (uses buffer as cushion) |

**Netflix's published results**: BBA reduced rebuffer rate by 10-20% compared to
throughput-based approaches, with improved perceptual quality due to fewer switches.

### 3.3 Hybrid ABR

Combines throughput estimation with buffer occupancy. Many modern players use this.

```
Hybrid Decision Matrix:

                    Buffer HIGH          Buffer LOW
                 +-------------------+-------------------+
  Throughput     | Highest quality   | Throughput-based  |
  HIGH           | (both signals     | selection         |
                 |  agree: go up)    | (trust throughput)|
                 +-------------------+-------------------+
  Throughput     | Buffer-based      | Lowest quality    |
  LOW            | selection         | (both signals     |
                 | (trust buffer)    |  agree: go down)  |
                 +-------------------+-------------------+
```

### 3.4 Contrast: Netflix vs YouTube ABR

| Aspect | Netflix | YouTube |
|---|---|---|
| Primary algorithm | Buffer-based (BBA) | Hybrid (throughput + buffer) |
| Content type | Professional, pre-encoded | UGC, highly variable quality |
| Initial quality | Higher (subscribers expect quality) | Lower (faster start, users abandon quickly) |
| Quality stability | Prioritized (fewer switches) | More aggressive switching |
| Encoding | Per-title optimized, extensive ladder | Fixed encoding ladder for most content |
| Tuning goal | Minimize rebuffer, maximize quality | Minimize time-to-first-frame, reduce abandonment |

YouTube tunes for faster start and lower initial quality because UGC viewers abandon
more quickly -- a 2-second delay in start loses ~6% of viewers. Netflix subscribers
are more patient but expect consistent quality.

---

## 4. Start-Up Optimization

The first few seconds of playback are critical. The player has no throughput history
and an empty buffer. The strategy:

```
Start-Up Phase Timeline:

Time  Action                              Buffer State
─────────────────────────────────────────────────────────
0.0s  Fetch manifest (MPD/M3U8)           [empty]
0.1s  Fetch init segment                  [empty]
0.3s  Fetch seg 0 @ 235 kbps (lowest)     [|...............]  ~4s
0.5s  BEGIN PLAYBACK                       Playing seg 0
0.7s  Fetch seg 1 @ 480 kbps              [|||.............]  ~8s
1.2s  Fetch seg 2 @ 1050 kbps             [|||||...........]  ~12s
2.0s  Fetch seg 3 @ 2100 kbps             [|||||||.........]  ~16s
3.5s  Fetch seg 4 @ 4300 kbps             [|||||||||.......]  ~20s
      ...ramp up continues...
```

### The Trade-Off

```
                         Start-Up Strategy Spectrum

  Fast start, low quality                          Slow start, high quality
  <─────────────────────────────────────────────────────────────────────>

  Start at 235 kbps              Start at 1050 kbps            Wait for
  Play in ~0.3s                  Play in ~1.0s                 buffer to
  Visible quality ramp           Moderate quality ramp          fill at 4K
                                                                3-5s delay

  Netflix mobile               Netflix TV app                  Not used
  (fast start matters)         (quality matters more)          (too slow)
```

Netflix varies start-up strategy by device:
- **Mobile**: Favor fast start (small screen hides low quality)
- **TV/4K**: Favor higher initial quality (large screen, quality is obvious)
- **Known fast connection**: Start at mid-tier quality

---

## 5. Mid-Stream Quality Switching

Quality switching happens at **segment boundaries** and is seamless because of how
segments are structured.

```
Segment N (1080p)                    Segment N+1 (480p)
+------+------+------+------+       +------+------+------+------+
| IDR  | P    | B    | P    |  -->  | IDR  | P    | B    | P    |
| frame| frame| frame| frame|       | frame| frame| frame| frame|
+------+------+------+------+       +------+------+------+------+
  ^                                   ^
  Keyframe: full image                Keyframe: full image at new resolution
  No dependency on                    No dependency on segment N
  previous segment                    Decoder handles resolution change
```

**IDR (Instantaneous Decoder Refresh) frame**: A special type of keyframe that
completely resets the decoder state. Every segment starts with one, which is why you
can switch quality (and resolution) at any segment boundary without glitches.

### What the viewer experiences

```
Playing 1080p                         Playing 480p
[crystal clear] [crystal clear] ... [slightly softer] [slightly softer] ...
                                   ^
                                   Switch point -- no freeze, no glitch
                                   Just a change in sharpness
```

The transition is not jarring because:
1. No rebuffer (the segment was fully downloaded before playback)
2. No decoding errors (IDR frame resets decoder state)
3. The resolution change is gradual to the eye (4 seconds of content per step)

---

## 6. DRM (Digital Rights Management)

Every segment is encrypted. The player must obtain a decryption key from a license
server before playback. Different platforms require different DRM systems.

### DRM Systems

| DRM System | Owner | Platforms | HD/4K Requirement |
|---|---|---|---|
| **Widevine** | Google | Android, Chrome, Chromecast, many smart TVs | L1 (hardware-backed TEE) for HD/4K; L3 (software) for SD only |
| **FairPlay** | Apple | iOS, Safari, macOS, Apple TV | Hardware-backed by default on Apple silicon |
| **PlayReady** | Microsoft | Windows (Edge), Xbox, many smart TVs | SL3000 (hardware) for HD/4K; SL2000 (software) for SD |

### Encryption Flow

```
Content Preparation (Offline):

  Raw Segment ──> AES-128-CTR Encryption ──> Encrypted Segment
                        ^                          |
                        |                          v
                   Content Key              Stored on S3/CDN
                        |
                        v
                   Key stored in
                   Netflix Key Server
```

```
Playback (Runtime):

Player                    License Server              CDN
  |                            |                       |
  |  1. Request license        |                       |
  |  (device cert + title ID)  |                       |
  |--------------------------->|                       |
  |                            |                       |
  |  2. Validate device        |                       |
  |     - Is device trusted?   |                       |
  |     - What security level? |                       |
  |     - Geographic rights?   |                       |
  |                            |                       |
  |  3. Issue license          |                       |
  |  (encrypted content key,   |                       |
  |   usage rules, expiry)     |                       |
  |<---------------------------|                       |
  |                            |                       |
  |  4. Fetch encrypted segment                        |
  |----------------------------------------------->   |
  |<-----------------------------------------------|   |
  |                                                    |
  |  5. Decrypt in TEE (hardware) or CDM (software)    |
  |  6. Decode and render                              |
```

### Key Properties

- **AES-128-CTR encryption**: Industry standard. CTR mode allows random access within
  a segment (no need to decrypt from the beginning).
- **Short-lived keys**: License has an expiration (hours to days). Device must
  re-request for continued playback. Prevents key sharing.
- **Device-bound**: The content key is encrypted to the specific device's certificate.
  Extracting the key requires breaking the hardware TEE.
- **CENC (Common Encryption)**: A standard that allows the same encrypted content to
  work with multiple DRM systems. Netflix encrypts once, and the same encrypted
  segments work with Widevine, FairPlay, and PlayReady -- only the license wrapping
  differs.

### Widevine Security Levels

```
+------------------------------------------------------------------+
|  L1 (Hardware)                                                    |
|  - Key handling AND decoding in Trusted Execution Environment     |
|  - Required for HD (720p+) and 4K on Netflix                     |
|  - Video never leaves secure hardware pipeline                   |
|  - Example: Qualcomm TrustZone on Android phones                 |
+------------------------------------------------------------------+

+------------------------------------------------------------------+
|  L3 (Software)                                                    |
|  - Key handling in software (CDM -- Content Decryption Module)    |
|  - Can be reverse-engineered (and regularly is)                   |
|  - Netflix limits to 480p SD                                      |
|  - Example: Chrome on desktop Linux                               |
+------------------------------------------------------------------+
```

### Netflix vs YouTube DRM

| Aspect | Netflix | YouTube |
|---|---|---|
| DRM systems used | Widevine + FairPlay + PlayReady | Primarily Widevine |
| Encryption | All content encrypted | Premium/paid content encrypted; free content uses obfuscation |
| HD/4K enforcement | Strict L1/hardware requirement | Similar for YouTube Premium |
| License model | Per-title, per-device | Per-session |
| Offline playback | Supported (time-limited license) | YouTube Premium only |

YouTube's free tier does not use full DRM for most content. Instead, it relies on
obfuscation techniques (signature-based URL tokens, short-lived URLs) that make
casual downloading harder but do not provide true content protection. Premium content
and paid movies use Widevine.

---

## 7. Segment Format: fMP4 (Fragmented MP4)

Netflix uses **fMP4 (fragmented MP4)** as the container format for segments. Unlike
regular MP4 (where metadata is at the start or end of the entire file), fMP4 embeds
metadata in each fragment.

### fMP4 Structure

```
Regular MP4:
+--------+--------------------------------------------------+
| moov   |                    mdat                           |
| (meta) |            (all media data)                       |
+--------+--------------------------------------------------+
  ^-- Must read this first, contains offsets for entire file
  ^-- Problem: can't start playback until moov is downloaded


Fragmented MP4 (fMP4):
+--------+  +------+--------+  +------+--------+  +------+--------+
| moov   |  | moof | mdat   |  | moof | mdat   |  | moof | mdat   |
| (init) |  | (hdr)| (data) |  | (hdr)| (data) |  | (hdr)| (data) |
+--------+  +------+--------+  +------+--------+  +------+--------+
  Init         Fragment 0         Fragment 1         Fragment 2
  Segment      (= Segment 0)     (= Segment 1)     (= Segment 2)
```

### Box Breakdown

| Box | Full Name | Contents |
|---|---|---|
| `moov` | Movie Box | Codec config (SPS/PPS), track info. Downloaded once as init segment. |
| `moof` | Movie Fragment Box | Timing, sample sizes, sample flags for this fragment. Lightweight header. |
| `mdat` | Media Data Box | Actual compressed video/audio frames for this fragment. |
| `styp` | Segment Type Box | Identifies the segment type (often precedes moof). |

Each segment = `styp` + `moof` + `mdat`. The `moof` tells the decoder exactly how
to parse the `mdat`, making each segment **self-contained** (given the init segment
has been loaded once).

### Why fMP4?

- **Streaming-friendly**: No need to download the entire file before playback
- **Standardized**: Works with both DASH and modern HLS (CMAF)
- **CENC-compatible**: Encryption metadata fits naturally in moof boxes
- **Efficient**: Minimal overhead per segment (~200-500 bytes of moof for a typical
  4-second segment containing ~2 MB of video data)

---

## End-to-End Flow: S3 to Screen

Putting it all together -- what happens when you press play:

```
Step  Action                                Who              What Happens
──────────────────────────────────────────────────────────────────────────────
 1    Press Play                            Client App       User clicks a title

 2    Fetch Playback Session                Netflix API      Returns: CDN URL, manifest
                                                             URL, license server URL,
                                                             device-specific config

 3    Fetch Manifest                        Client -> CDN    GET manifest.mpd (DASH)
                                                             or master.m3u8 (HLS)
                                                             Parses available quality
                                                             levels

 4    Request DRM License                   Client -> KMS    Sends device cert + title ID
                                                             Receives encrypted content key

 5    Fetch Init Segment                    Client -> CDN    GET init.mp4
                                                             Configures decoder with
                                                             codec parameters

 6    Fetch Segment 0 (low quality)         Client -> CDN    GET seg_0.m4s at 235 kbps
                                                             ABR picks lowest for fast start

 7    Decrypt + Decode + Render             Client (TEE)     AES-128 decrypt in hardware,
                                                             H.264/H.265/AV1 decode,
                                                             render to screen

 8    Begin Playback                        Client App       Video appears on screen
                                                             (~0.5-1.5s after step 1)

 9    Fetch Segment 1 (higher quality)      Client -> CDN    ABR sees buffer growing,
                                                             picks higher bitrate

 10   Continue: ABR loop                    Client           For each segment:
                                                             - Check buffer level (BBA)
                                                             - Pick quality
                                                             - Download, decrypt, decode
                                                             - Repeat until end of title
```

```
                                    +------------------+
                                    |   Netflix API    |
                                    | (playback session|
                                    |  manifest URLs)  |
                                    +--------+---------+
                                             |
                                             | 2. Session info
                                             v
+----------------+    3. Manifest     +------+--------+
|                |<-------------------|               |
|   CDN Edge     |    5. Init seg     |    Player     |
|   (S3 origin)  |<-------------------|   (Client)    |
|                |    6-10. Segments   |               |
|                |------------------->|  +----------+  |
+----------------+                    |  |ABR Engine|  |
                                      |  +----------+  |
                                      |  |DRM Module|  |
                                      |  +----------+  |
                                      |  | Decoder  |  |
+----------------+    4. License      |  +----------+  |
|  License       |<-------------------|  | Renderer |  |
|  Server (KMS)  |------------------->|  +----------+  |
+----------------+   Content key      +----------------+
                                             |
                                             v
                                         [Screen]
```

---

## Summary

| Component | Purpose | Netflix's Choice |
|---|---|---|
| Segmentation | Enable per-segment quality decisions | 4-second segments, keyframe-aligned |
| Manifest | Describe available qualities and segment URLs | DASH (MPD) primary, HLS for Apple |
| ABR Algorithm | Pick optimal quality for each segment | Buffer-based (BBA) |
| Start-up | Minimize time-to-first-frame | Low quality first, ramp up |
| Quality switching | Seamless transitions between bitrates | At segment boundaries via IDR frames |
| DRM | Protect content from unauthorized copying | Widevine + FairPlay + PlayReady with CENC |
| Container format | Package compressed frames for streaming | fMP4 (fragmented MP4) |
