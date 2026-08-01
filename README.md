# ALR Quote Verifier

ALR Quote Verifier reads footnotes from Microsoft Word documents, separates
their citations, resolves supported sources, checks quoted text, and produces
an Excel workbook for human review. Canadian cases, legislation, and ALR journal 
articles get linked automatically, including a direct link right to the relevant
page, paragraph, or section where possible. Using [A2AJ's free API](https://a2aj.ca/data/), quotes get checked against their sources: 
you can see if the supplied quote matches the source, and whether it's at the pinpoint specified,
or at a different pinpoint. If it's partially matching, you see what parts of the quote were changed.
This saves enormous time in the law review editing process, and may assist other legal research
endeavours as well. An experimental feature allows analysis of PDFs which would, for example,
allow a lawyer to quickly review the sources in an opposing litigant's brief.

## Quick start for Windows

For non-technical users:

1. Download `ALR-Quote-Verifier-windows-x64.zip` and its matching `.sha256` file from the [latest release](https://github.com/AlbertaLawReview/ALR-Verifier/releases/latest).
2. Right-click the ZIP file, choose **Extract All**, and open the extracted folder. There is no separate installer.
3. Double-click **ALR Quote Verifier.exe**.
4. If Windows shows **Windows protected your PC**, click **More info** and then **Run anyway** only if you downloaded this release from this page and the app name is correct. If Defender reports a virus or other threat, cancel and do not run it.
5. Use **Add files** to select your Word `.docx` files, choose your settings, and press **Run**. Finished Excel workbooks are written to the `CHECKED_EDITS` folder.
6. Modes that use AI ask for your OpenAI API key. The app includes a **First-time setup guide** and a masked **Set key** dialog, so you can follow the setup inside the software; you do not need to edit files or use the command line. The key is encrypted for your Windows account. OpenAI bills usage under your account. **Free** mode makes no AI calls.

## Example workbook

![Generated workbook showing a checked quotation marked Perfect Match](https://raw.githubusercontent.com/AlbertaLawReview/ALR-Verifier/main/assets/workbook-example.png)
## Download details

Download the latest ZIP and its SHA-256 checksum from the
[GitHub releases page](https://github.com/AlbertaLawReview/ALR-Verifier/releases/latest).
Keep the files together and verify the checksum before running the application.

## Run from source (technical)

Python 3.11 or newer is recommended. Tk must be available in the Python
installation.

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    python gui.py

On macOS or Linux, activate the environment with
source .venv/bin/activate. Use **Add files** to select one or more DOCX files,
choose the settings, and press Run. Review workbooks are written to
CHECKED_EDITS by default.

PDF intake is experimental. It uses the deterministic legal-PDF engine for
native text, page structure, footnotes, endnotes, and cross-page note bodies.
Enable it under **Settings → Advanced → Enable experimental PDF input**.
Image-only and scanned PDFs are not supported. Unusual layouts should be
reviewed carefully.

An OpenAI API key is required for modes that use AI. Enter it when prompted or
set OPENAI_API_KEY. A key entered in the Windows application is encrypted for
the current Windows user. API usage is billed by OpenAI under your account.

The source tree includes the journal matching and quotation-checking code. The
Windows package includes an Alberta Law Review journal database.

## Processing modes

| Mode | What it does | About 20 pages | About 1,000 pages per year |
| --- | --- | ---: | ---: |
| High accuracy | Uses AI to read every footnote. | $0.75 | $38 |
| Economy | Handles straightforward supra and ibid footnotes without AI. | $0.72 | $36 |
| Ultra economy | Handles clearly structured citations without AI and uses AI when important details are uncertain. | $0.70 | $35 |
| Free | Makes no AI calls; uncertain citations may remain together and linking is less accurate. | $0 | $0 |

The independent Supra linking setting applies in every mode. Safe makes only
high-confidence links. Aggressive also tries two limited ways to recognize
references that Safe leaves unresolved. Safe is the default.

These rough estimates use the GPT-5.2 rates and 20-page test document measured
in July 2026. Actual API cost varies with citation density and model pricing.
AI modes currently use GPT-5.2. We compared it under the production workflow
with DeepSeek Flash 0731 and GPT-5.6 Luna, Terra, and Sol. GPT-5.2 was the
best cost-and-accuracy balance: cheaper Luna runs were materially less
accurate, while Terra and Sol matched or exceeded its accuracy only at a much
higher price, including their lower-effort tiers.

## Network access and privacy

Depending on the selected mode and source settings, the application may send
citation or quoted text to OpenAI and may query A2AJ, CourtListener, GovInfo,
the UK National Archives, GOV.UK, and linked source sites. Do not process
confidential material unless that use is permitted by your obligations and by
the applicable service terms. Review each service's current privacy and data
retention policies yourself.

The **A2AJ local corpus** panel can install the complete case-law and
legislation datasets for faster local-first lookups. It checks upstream
partition metadata for staleness, resumes interrupted downloads, and reuses
unchanged partitions. After a download it atomically builds the shared SQLite
runtime used for every lookup. The separate **Local only** setting requires
that complete corpus and prevents verification runs from making network
requests; journal retrieval and the bundled reference database are already
local. Installing or updating the corpus is an explicit network operation. It
downloads about 4.9 GB compressed and requires approximately 10 GB of free
disk space while the local SQLite runtime is built. The corpus feature is
included in the normal application and source installation; no separate
importer setup is needed.

## Windows package

The downloadable Windows package contains ALR Quote Verifier.exe, the
Alberta Law Review journal database, the project licence, applicable upstream
notices, and a SHA-256 checksum. The executable is digitally signed.

## Test

    python -m pip install -r requirements.txt pytest
    python -X utf8 -m pytest tests -q

CI runs the test suite on Windows, macOS, and Linux with Python 3.11.

### PDF parsing

The legal-PDF parser is included directly in this repository as a fixed,
self-contained snapshot. The application uses that copy directly; normal
clones, tests, and builds do not require another engine checkout or a sync
step.

## Key contributors

- Eli Ziff
- Martin Rudolf

## License

Copyright 2026 Alberta Law Review.

Project-authored code is licensed under Apache License 2.0. Upstream licenses,
notices, and service terms still apply. See LICENSE, NOTICE, and
THIRD_PARTY_NOTICES.md.

Report security concerns privately as described in SECURITY.md.
