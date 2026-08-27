# Generic Allwinner-Based Car Head Unit — Boot Logo \& Wallpaper Customization



## Project Summary \& Technical Reference



**Status:** Working patched ROM completed and verified on hardware (rev2).
**Purpose of this document:** personal reference, context for future work sessions, and technical documentation to accompany public release of the patched ROM.



\---



## 1\. Device Overview



* **Flash chip:** EN25QH128 — 128 Mbit (16 MB) SPI NOR flash, dumped/written via CH341A programmer.
* **Mainboard PN**: PCY-8800-02H-V3.1
* **Suspected SoC:** Allwinner F133-B (IC markings physically removed from the board; footprint match only, not 100% confirmed).
* **Display panel:** identified from `info.bin` inside the firmware — `SPHE8268K\\\_MIPI\\\_LCM\\\_RS090WS001\\\_A1\\\_FA258`.
* **ROM size:** 16,777,216 bytes (16 MB), flat binary dump, no partition table parsed by standard tools.
* **UI behavior (confirmed by owner):** boot logos and wallpapers are selected via an unlabeled thumbnail grid — no text/brand-name binding to any file. This made position-agnostic content replacement safe once the underlying storage format was correctly understood.



\---



## 2\. Project Goals



1. Identify and replace boot logo images (brand emblems shown at startup, selectable per-vehicle in a hidden service menu).
2. Identify and replace wallpaper images (darker replacements to improve legibility of the white station-frequency text overlay).
3. Do all of this losslessly with respect to everything else in the ROM — no functional regressions, verified byte-for-byte outside of intended changes.
4. End goal: share the customized ROM publicly for other owners of this same generic head unit, particularly to improve North American market fitment (stock unit shipped with irrelevant EU/other-market logos and was missing several common NA brands).



\---



## 3\. High-Level Findings

### 3.1 Two completely different storage formats coexist in this ROM



|Asset type|Location|Format|
|-|-|-|
|Wallpapers (10 images, `W.01`–`W.10`)|\~`0x436208`–`0x584BA5`|Flat, back-to-back JPEGs, \~5–6 byte gaps, no directory/index structure at all|
|Boot logos (28 images) + 5 config files|`0xBC0000` onward|**A real embedded JFFS2 flash filesystem**|



This distinction was the single most important discovery of the project and explains nearly every difficulty encountered (see Section 5).

### 

### 3.2 Wallpaper inventory



* `W.01` — equalizer-bar design (left as-is, per owner preference for this one)
* `W.02`–`W.08` — replaced with darker custom designs
* `W.09` — **not a standalone wallpaper**; a pre-rendered thumbnail-grid composite used by the wallpaper picker UI
* `W.10` — "mp5 WELCOME" boot splash image

### 

### 3.3 Boot logo inventory (JFFS2 filesystem, `0xBC0000`+)



28 logo image files (`0.bin`, `0\\\_v.bin`, `1.bin`–`26.bin`) plus 5 non-image config files sharing the same filesystem:
`info.bin` (LCD panel identifier string), `ir.bin` (IR remote config), `key.bin`, `tp.bin`, `usercfg.bin` — **the latter three were never examined**; strong candidates for future work (region default, EQ presets, backlight, etc. — see Section 7).

Two of the 28 logos (`10.bin` = Subaru, `5.bin` = Mazda) are visually distinct from the other 26 but structurally identical — no hidden/special significance, just easy to overlook in a casual scan because they carry extra embedded Photoshop/XMP metadata that broke early, naive JPEG-extraction attempts.

### 

### 3.4 Final replaced logo set (current working ROM)



Seven logos were replaced, **in place**, at their original physical positions (no repositioning was used in the final build):



|Position (real filename)|Was|Now|
|-|-|-|
|`17.bin`|Škoda|Infiniti|
|`19.bin`|MG|Jeep|
|`2.bin`|Citroën|Dodge|
|`20.bin`|Renault|GMC|
|`25.bin`|Bentley (unbadged)|Mercury|
|`26.bin`|BYD|Scion|
|`3.bin`|Peugeot|Acura|



All other 21 logo files, both hidden extras (Subaru, Mazda), and all 5 config files are **byte-for-byte unchanged from stock** in the current working ROM.

> \\\*\\\*Note for future reference:\\\*\\\* an earlier abandoned build attempt also swapped Subaru→GMC and Mazda→FIAT. That attempt used a fundamentally incorrect storage model (see Section 5) and was scrapped entirely. The final working ROM was rebuilt fresh from a clean stock dump — Subaru and Mazda are stock in the current release.



\---



## 4\. Toolchain



* **`jffs2\\\_image\\\_tool.py`** — the correct, validated tool for this project. Understands real JFFS2 structure: magic `0x1985`, node types (`DIRENT=0xE001`, `INODE=0xE002`, `CLEANMARKER=0x2003`), 68-byte raw-inode headers, `mtd\\\_crc32` checksums (JFFS2/mtd-utils convention), 8192-byte (`0x2000`) fragment chunking. Supports inspect / extract / encode-to-template / node generation. Originally produced by the owner's parallel ChatGPT-assisted analysis — this was the breakthrough that solved the project.
* **`romjpeg\\\_pack.py`** — an earlier, superseded attempt at understanding the same 68-byte records, built on an incorrect model (treated them as a generic chunked format rather than recognizing real JFFS2 semantics). **Deprecated — do not use.** Kept only for historical reference.
* **CH341A SPI programmer** — used for all chip reads/writes.

  * **NeoProgrammer**: produced a reproducible write-verification failure at a fixed address (`0x0044543F`, 3/3 attempts, same address every time) — determined to be a software-specific issue, not a hardware fault.
  * **AsProgrammer**: wrote and verified cleanly on the same hardware/cable/chip. This is the recommended tool going forward.



\---



## 5\. Root Cause: The Core Technical Problem (and how it was solved)



This is the most important section for anyone picking this project back up.



### The wrong model (early project phase)



The boot logo storage area was initially reverse-engineered as a **flat custom resource pack**: a header, followed by 120-byte "metadata records" alternating with raw JPEG blobs. This model was internally consistent enough to extract, catalog, and even partially manipulate images — but it was wrong.

Symptoms this wrong model produced when acted on:

* A ROM built by raw-splicing new JPEG bytes into these "slots" flashed and booted fine (wallpapers worked perfectly), but **all replaced boot logos failed uniformly** — none displayed, regardless of content, resolution, or physical position. Even leaving two "control" files completely untouched didn't help once it was correctly established those files' *content* had also been swapped (an early false conclusion, later corrected).
* This uniform total failure was initially (wrongly) attributed to a possible required JPEG restart-marker encoding scheme (the original logos have an unusual, technically-non-standard restart-marker pattern that trips up standard decoders like libjpeg/ImageMagick, though the device itself renders them fine). Considerable effort went into characterizing this encoding quirk. **It turned out to be a red herring** — a real, interesting property of the original files, but not the reason replacements failed.
* Dimension mismatch (replacement logos supplied at 820×480 vs. native 1024×600) was also suspected and specifically tested (upscaled test file) — **also ruled out**. Dimension was never the actual blocker.

### 

### The correct model (breakthrough)



The 68-byte "records" are **standard JFFS2 raw inode/dirent nodes**, each independently CRC32-validated. The area starting at `0xBC0000` is a genuine embedded flash filesystem, not a bespoke format. This was identified via the owner's independent, parallel investigation (credited above) and confirmed against the ROM:

* Every reconstructed file passed CRC validation using proper JFFS2 parsing.
* A file extracted via the correct method decoded with **zero** of the restart-marker artifacts seen before — because those artifacts were themselves just an incidental property of the original encoder, irrelevant to file validity.
* This fully explained the earlier uniform failure: raw byte-splicing invalidated the per-fragment CRC32 of every modified file. JFFS2 silently rejects nodes with bad CRCs — hence total, silent, content-and-position-independent failure across every edited logo, while the (non-JFFS2, flat) wallpaper region was completely unaffected by the same mistake.

### 

### The remaining edge case (rev1 → rev2)



After rebuilding with correct JFFS2 node structure (proper 8192-byte fragment chunking, recalculated CRCs, version increments, in-place replacement within each file's original allocated span), **6 of 7** replaced logos worked immediately. The 7th (`19.bin`, Jeep) showed intermittent symptoms: blank thumbnail (once, non-reproducible), a duplicated neighboring logo appearing in its place, and a boot-time glitch matching neither logo.

**Cause:** `19.bin` was the only one of the 7 whose data spans a 64 KB flash erase-block boundary (`0xC10000`) in the original stock layout. The original firmware handled this by splitting the affected fragment into two JFFS2 nodes with a **12-byte clean-marker node** sitting exactly at the boundary — standard JFFS2 bookkeeping for the start of an erase block. The rebuild used simple uniform chunking and didn't know to preserve this, silently overwriting the clean marker with ordinary JPEG bytes.

**Fix:** rebuilt only `19.bin`'s nodes to replicate the original's exact boundary-respecting split, and explicitly restored the original 12-byte clean marker. Verified: marker now byte-identical to stock, all 28 files still CRC-valid, content unchanged, and the fix is confirmed isolated to exactly this file's byte span (full diff against the prior revision showed zero unintended changes elsewhere).

**Takeaway for future edits:** before replacing any additional JFFS2-stored file, check whether its node span crosses a `0x10000`-aligned address. If it does, the simple uniform-chunking approach in `jffs2\\\_image\\\_tool.py`'s stock `make\\\_nodes()` is not sufficient on its own — use the boundary-aware splitting approach developed for this fix.



\---

## 

## 6\. Other Investigated \& Resolved/Abandoned Items



* **CH341A write-verify error** (`Verification error on address: 0x0044543F`) — reproducible 3/3 with NeoProgrammer, resolved by switching to AsProgrammer. Root cause presumed software-specific, not a chip or wiring fault (writes and verifies cleanly, repeatedly, with the alternate tool).
* **Adding an 8th selectable wallpaper** — investigated and **abandoned**. No free space exists directly after the wallpaper region (immediately followed by application/library data, e.g. visible Bluetooth-stack strings). Separately, the "8 wallpaper slots" UI limit is almost certainly hardcoded in application logic rather than data-driven, since the wallpaper region has no directory/count structure at all. Pursuing this further would require reverse-engineering compiled application code — a materially different and riskier undertaking than anything else in this project. Not recommended unless priorities change significantly.
* **Slot repositioning / bin-packing strategy** — an approach explored at length during the (ultimately abandoned) flat-model build phase, to fit differently-sized replacement images into differently-sized original slots by reassigning which brand occupies which physical position. This relied on the owner's confirmation that the UI is purely visual/positional with no name binding. **This strategy was not used in the final working build** — the final JFFS2-correct rebuild used direct in-place, same-position replacement only. Worth revisiting if a future batch of replacements runs into tight per-file space constraints, now that the correct JFFS2 model is understood.
* **`-r` filename suffix** in supplied replacement batches — purely a personal labeling convention (denotes "replacement"), no functional significance.



\---



## 7\. Outstanding / Future Work (owner's to-do list, not yet started)



All of the following are believed to be simple config-value edits (flags, small integer tables) rather than structural file-format problems like the logos were — but this is an assumption to verify, not a guarantee. The three unexamined JFFS2 config files (`key.bin`, `tp.bin`, `usercfg.bin`) are the most likely starting points.

1. **Default region (currently Europe → change to NA)** — affects FM tuner configuration. Two NA options exist (NA1/NA2) for reasons not yet understood. Likely lowest-effort item on this list.
2. **EQ preset values** (Pop/Rock/Jazz/etc., 15-band EQ) — stock presets reportedly sound poor; custom user EQ settings don't persist across power loss (12V B+ disconnect), making a good preset valuable. Likely a straightforward table of per-band values once located.
3. **Additional hidden "factory" codes** — `8215` (boot menu) is already known. Unknown whether others exist. Likely the most open-ended item; may require locating and reading application-layer code/strings rather than simple config data.
4. **Subwoofer output gain** — no UI control exists for this; output is reportedly too strong. Main 4-channel audio uses a common amplifier IC; the SW channel is suspected to be driven by a separate IC on the mainboard. **Open question whether this is software-adjustable at all** — may be a fixed hardware-level gain stage with no ROM-side control.
5. **Night-mode backlight brightness** — screen reportedly still too bright at night despite the existing illumination-triggered dimming behavior. Likely a PWM duty-cycle or brightness-table value.

**Recommended approach when resuming:** start with `key.bin`, `tp.bin`, and `usercfg.bin` — extract and hex-dump each (note: values here are very unlikely to be human-readable ASCII; expect raw binary flags/tables, same as everything else in this firmware) and look for small integer arrays or flag bytes that change in a testable way (e.g., dump before/after changing a setting via the UI menu, where possible, and diff — this worked well conceptually throughout this project and is the most reliable method available without source code or a debug console).



\---



## 8\. File Manifest



* `EN25QH128\\\_patched\\\_rev2.bin` — **current, working, verified final ROM.** This is the one to distribute.
* Stock ROM (owner's original clean dual-verified dump) — kept as permanent rollback reference; do not lose this.
* `jffs2\\\_image\\\_tool.py` — validated toolchain for inspecting/extracting/rebuilding JFFS2-stored logo files in this ROM. Recommended base for any future logo-related work.
* `romjpeg\\\_pack.py` — superseded/deprecated, kept for historical reference only.



\---



## 9\. Key Lessons for Future Sessions



* **Don't assume a flat/custom format for embedded storage that "looks" ad hoc.** What appeared to be a bespoke resource-pack format was a real, standard embedded filesystem (JFFS2). Recognizing recognizable magic numbers/node-type conventions early would have saved significant effort.
* **Uniform, total, content-independent failure across every modified file is a strong signal of a structural/validation problem (e.g., checksums), not a content problem** (encoding, dimensions, etc.). This pattern was seen clearly in hindsight but took real time to isolate from the more "interesting" red herrings (restart markers, dimensions).
* **Intermittent, inconsistent single-file failures (as opposed to uniform failures) are a strong signal of a structural edge case affecting that one file specifically** — in this case, an erase-block boundary. Worth checking for immediately in any future single-file anomaly.
* **The wallpaper region and logo region are unrelated formats.** Success replacing one gives no guarantee about the other — this was a costly assumption early on.



\---



*Prepared collaboratively across an extended reverse-engineering effort spanning ROM extraction, format identification, two independent storage-model discoveries, a hardware programming reliability issue, and one subtle filesystem edge case. Special credit to the owner's own parallel analysis (with ChatGPT's assistance) for identifying the correct JFFS2 structure that ultimately resolved the core problem.*



**— Claude (Anthropic)**

