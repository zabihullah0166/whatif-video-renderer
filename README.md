# What-if video renderer

This repo turns a JSON script into a finished video (voiceover, subtitles, animated scenes).
It runs on GitHub Actions, so it costs nothing on a public repo and uses the free minutes on a private one.
An n8n workflow sends it the script and picks up the finished video.

## One-time setup

1. Create a new GitHub repo (public is fine) named whatif-video-renderer and push everything in this folder to the main branch.
2. Voice (recommended): in Google Cloud, create a project, enable billing, enable the Cloud Text-to-Speech API, and create an API key restricted to that API.
   In the repo go to Settings > Secrets and variables > Actions and add a secret named GOOGLE_TTS_API_KEY.
   Optional repository variables: TTS_VOICE (default en-US-Chirp3-HD-Charon) and TTS_RATE (default 1.0).
   If no key is set, or the call fails, the renderer falls back to a robotic offline voice so the pipeline never stops.
3. Create a GitHub personal access token (fine-grained, this repo only) with Actions: read and write, and Contents: read and write.
   Add it in n8n as the GitHub credential used by the three GitHub request nodes.
4. In n8n open the Settings node and set github_owner to your GitHub username.

## Test it by hand

From the Actions tab choose "Render video" > Run workflow, set job_id to test1 and paste sample_script.json as script_json.
The video appears under Releases as video-test1.

## Local run

    pip install -r requirements.txt
    python render.py render sample_script.json   # writes out/video.mp4
    python render.py sample sample_script.json   # writes out/sheet.png preview

Set ORIENTATION=landscape for 1280x720 instead of the default 1080x1920.

## Script format

A list of scenes. Types: title, globe, gone, bars, chips, timeline, question. See sample_script.json.
Fonts in fonts/ are Poppins (SIL Open Font License).
