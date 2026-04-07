from __future__ import annotations

import asyncio
import logging
import math
import re
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tunedrop.app.core.client import get_aiogram_bot, get_pyrogram_client
from tunedrop.app.core.config import settings
from tunedrop.app.core.database import get_database
from tunedrop.app.services.link_generator import link_store
from tunedrop.app.utils.memory_cache import MemoryCache

logger = logging.getLogger(__name__)

# Cache decoded FileId objects — avoids repeated FileId.decode() per request
_fileid_cache = MemoryCache(max_size=5000, ttl=1800.0)  # 30 min TTL

# ── SEO Page Data ──────────────────────────────────────────────────────────

_SEO_PAGES: dict[str, dict] = {
    "/spotify-to-mp3": {
        "page_title": "Spotify to MP3 Converter — Download Free 320kbps | TuneDrop",
        "page_desc": "Convert any Spotify song, album, or playlist to MP3 instantly. Free, no login, 320kbps quality. Try TuneDrop's Spotify to MP3 downloader now.",
        "h1_title": "Spotify to MP3 Converter — <span class='gradient-text'>Free & Instant</span>",
        "h1_sub": "Convert any Spotify song, album, or playlist to high-quality 320kbps MP3 in seconds. No login required.",
        "content": """
          <h2>Download Spotify Songs to MP3 — Free</h2>
          <p>TuneDrop is the easiest way to <strong>convert Spotify to MP3</strong>. Whether you want a single track, a full album, or an entire playlist, TuneDrop handles it all — completely free and with no account required. Just send a Spotify link to our <a href="/telegram-music-bot">Telegram music bot</a> and get a high-quality 320kbps MP3 file back in seconds.</p>
          <p>Unlike other Spotify to MP3 converters that require software downloads, browser extensions, or email signups, TuneDrop works entirely inside Telegram. Your music is delivered directly in your chat — ready to save, forward to friends, or listen to offline. No Spotify premium account needed either. Just paste a link and download.</p>

          <h2>How to Convert Spotify to MP3 in 3 Steps</h2>
          <ol>
            <li><strong>Open TuneDrop</strong> on Telegram — search for @TuneDropOfficialBot and press Start</li>
            <li><strong>Paste any Spotify link</strong> — a track, album, or <a href="/download-spotify-playlist">playlist URL</a></li>
            <li><strong>Get your MP3 instantly</strong> — delivered as a 320kbps file with cover art embedded</li>
          </ol>
          <p>The entire Spotify to MP3 conversion takes just a few seconds. No complicated steps, no waiting for email confirmations, no downloading desktop software. You can also use the <code>/song</code> command to search by name — type <code>/song Bohemian Rhapsody</code> and TuneDrop finds and downloads it instantly.</p>

          <h2>Why TuneDrop Is the Best Spotify to MP3 Converter</h2>
          <ul>
            <li><strong>320kbps audio quality</strong> — the highest standard MP3 quality available, far better than 128kbps converters</li>
            <li><strong>Full metadata</strong> — every track includes artist name, album name, track number, and ID3 tags</li>
            <li><strong>Album art included</strong> — embedded cover art for every download, so your library looks great</li>
            <li><strong>No download limits</strong> — download as many Spotify songs as you want, forever free</li>
            <li><strong>Playlist to ZIP</strong> — <a href="/download-spotify-playlist">convert entire Spotify playlists</a> to a single ZIP file</li>
            <li><strong>No account needed</strong> — just open Telegram and start downloading immediately</li>
            <li><strong>Works on all devices</strong> — iOS, Android, Windows, Mac — anywhere Telegram runs</li>
            <li><strong>Smart caching</strong> — repeat downloads are served instantly from cache</li>
          </ul>

          <h2>Spotify Playlist to MP3</h2>
          <p>Want to download a full Spotify playlist? Send any playlist URL to TuneDrop and receive all tracks packed into a single ZIP file. Each song is individually converted to MP3 with proper metadata — artist name, album art, track numbers — so your music library stays perfectly organized. Whether it's 10 tracks or 500, TuneDrop handles it with concurrent processing for maximum speed.</p>
          <p>Check our detailed guide on <a href="/download-spotify-playlist">how to download Spotify playlists as MP3</a> for step-by-step instructions and tips.</p>

          <h2>Spotify to MP3 — No Premium Account Required</h2>
          <p>You don't need a Spotify Premium subscription to use TuneDrop. Any public Spotify link works — free Spotify users, premium users, even people who don't have a Spotify account at all. Just copy the link from Spotify's share menu and paste it into the TuneDrop bot. The bot handles everything from there, including finding the right version, downloading, converting, and tagging.</p>

          <h2>TuneDrop vs Other Spotify to MP3 Converters</h2>
          <p>Most Spotify to MP3 tools are web-based converters cluttered with ads, popups, and suspicious redirects. Some require you to download desktop software that may contain malware. Others limit you to 128kbps quality or cap your daily downloads.</p>
          <p>TuneDrop is different: it runs inside Telegram (a trusted app with 800M+ users), delivers 320kbps quality every time, has zero ads, and never limits your downloads. It's the safest and fastest way to convert Spotify to MP3.</p>
          <p>Also check out our <a href="/youtube-to-mp3">YouTube to MP3 converter</a> for converting YouTube Music links.</p>

          <h2>Frequently Asked Questions</h2>
          <h3>Can I download Spotify songs without premium?</h3>
          <p>Yes! TuneDrop works with any Spotify link — free or premium. You don't need a Spotify account at all. Just paste the link and download your MP3.</p>
          <h3>What Spotify link formats are supported?</h3>
          <p>TuneDrop supports all Spotify link types: individual track links, full album links, playlist links, and Spotify URIs (spotify:track:...). Any shareable Spotify URL works.</p>
          <h3>What quality are the MP3 files?</h3>
          <p>All Spotify to MP3 conversions produce 320kbps MP3 files — the highest standard audio quality. Every file includes embedded cover art and complete ID3 metadata tags.</p>
          <h3>Is it safe to use TuneDrop?</h3>
          <p>Absolutely. TuneDrop runs inside Telegram — no software to install, no websites to visit, no personal data collected. Your downloads are private and delivered through Telegram's encrypted chat.</p>
          <h3>Can I download Spotify albums?</h3>
          <p>Yes! Send any Spotify album link and TuneDrop downloads every track individually. For full albums, we recommend using the playlist approach — all songs come organized with track numbers.</p>
        """,
        "schema_extra": """
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://tdrp.cc/"},
              {"@type": "ListItem", "position": 2, "name": "Spotify to MP3", "item": "https://tdrp.cc/spotify-to-mp3"}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
              {"@type": "Question", "name": "Can I download Spotify songs without premium?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! TuneDrop works with any Spotify link — free or premium. You don't need a Spotify account. Just paste the link and download."}},
              {"@type": "Question", "name": "What Spotify link formats are supported?", "acceptedAnswer": {"@type": "Answer", "text": "TuneDrop supports all Spotify link types: track links, album links, playlist links, and Spotify URIs. Any shareable URL works."}},
              {"@type": "Question", "name": "What quality are the MP3 files?", "acceptedAnswer": {"@type": "Answer", "text": "All conversions produce 320kbps MP3 files with embedded cover art and complete ID3 metadata."}},
              {"@type": "Question", "name": "Is it safe to use TuneDrop?", "acceptedAnswer": {"@type": "Answer", "text": "Yes. TuneDrop runs inside Telegram — no software to install, no websites to visit, no personal data collected."}},
              {"@type": "Question", "name": "Can I download Spotify albums?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! Send any Spotify album link and every track is downloaded individually with proper metadata and track numbers."}}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "name": "How to Convert Spotify to MP3",
            "description": "Convert any Spotify song to MP3 using TuneDrop on Telegram.",
            "step": [
              {"@type": "HowToStep", "name": "Open TuneDrop", "text": "Search for @TuneDropOfficialBot on Telegram and press Start."},
              {"@type": "HowToStep", "name": "Paste Spotify Link", "text": "Copy any Spotify track, album, or playlist URL and paste it in the chat."},
              {"@type": "HowToStep", "name": "Download MP3", "text": "Receive a 320kbps MP3 file with cover art delivered instantly in your chat."}
            ]
          }
          </script>
        """,
    },
    "/youtube-to-mp3": {
        "page_title": "YouTube to MP3 Downloader — Free 320kbps | TuneDrop",
        "page_desc": "Convert YouTube Music videos to MP3 instantly. Free Telegram bot — 320kbps quality, no signup, playlists as ZIP. Try TuneDrop now.",
        "h1_title": "YouTube to MP3 Downloader — <span class='gradient-text'>Free & Fast</span>",
        "h1_sub": "Convert any YouTube Music video or playlist to 320kbps MP3 instantly. No login required.",
        "content": """
          <h2>Download YouTube Music to MP3 — Free</h2>
          <p>TuneDrop makes converting <strong>YouTube to MP3</strong> incredibly simple. Send any YouTube Music link to our <a href="/telegram-music-bot">Telegram bot</a> and receive a high-quality 320kbps MP3 file in seconds. No software to install, no accounts to create — just fast, free YouTube to MP3 conversion inside the messaging app you already use.</p>
          <p>Whether you're converting a single music video, a full album upload, or an entire YouTube playlist, TuneDrop handles it all. Every download includes cover art, artist information, and properly formatted ID3 tags so your music library stays organized automatically.</p>

          <h2>How to Convert YouTube to MP3</h2>
          <ol>
            <li><strong>Open TuneDrop</strong> on Telegram — search @TuneDropOfficialBot and press Start</li>
            <li><strong>Paste a YouTube Music link</strong> — any video or playlist URL works</li>
            <li><strong>Download your MP3</strong> — 320kbps with cover art, delivered instantly in your chat</li>
          </ol>
          <p>You can also use the <code>/song</code> command to search for any song by name — no need to find the YouTube link first. Type <code>/song Blinding Lights</code> and TuneDrop finds and downloads the best version for you. It's the fastest way to go from hearing a song to having it on your phone.</p>

          <h2>Why TuneDrop Is the Best YouTube to MP3 Converter</h2>
          <ul>
            <li><strong>320kbps quality</strong> — highest standard MP3 audio, not the compressed 128kbps other tools offer</li>
            <li><strong>Instant delivery</strong> — MP3 sent directly in your Telegram chat, no redirect pages</li>
            <li><strong>Playlist downloads</strong> — convert entire YouTube playlists to individual MP3 files in a ZIP</li>
            <li><strong>No installation</strong> — works inside Telegram, no suspicious APKs or desktop software</li>
            <li><strong>Search by name</strong> — use <code>/song</code> to find and download without even having a link</li>
            <li><strong>Complete metadata</strong> — cover art, artist name, album info automatically tagged</li>
            <li><strong>100% free forever</strong> — unlimited downloads, no subscriptions, no daily caps</li>
            <li><strong>No ads</strong> — zero popups, zero redirects, unlike web-based converters</li>
          </ul>

          <h2>YouTube Playlist to MP3</h2>
          <p>Sending a YouTube Music playlist URL to TuneDrop gives you all tracks packed into a single ZIP file. Each song is individually converted to MP3 with proper metadata and cover art. Perfect for building your offline music library from YouTube playlists of any size.</p>
          <p>TuneDrop also supports <a href="/spotify-to-mp3">Spotify to MP3 conversion</a> — one bot for both platforms.</p>

          <h2>YouTube to MP3 on iPhone and Android</h2>
          <p>TuneDrop works on any device that runs Telegram — iOS, Android, Windows, Mac, and Linux. There's no need to install a separate YouTube to MP3 app or visit sketchy websites. Just open Telegram, send a link to the TuneDrop bot, and download your MP3 directly to your phone. The file saves right to your device's music folder.</p>

          <h2>TuneDrop vs Web-Based YouTube to MP3 Sites</h2>
          <p>Most YouTube to MP3 websites are filled with intrusive ads, fake download buttons, and sometimes malware. They often redirect you through multiple pages before giving you the file — if they even work at all. Many have been shut down or are blocked by ISPs.</p>
          <p>TuneDrop bypasses all of that. It runs inside Telegram — a trusted app with end-to-end encryption. No ads, no redirects, no sketchy websites. Your MP3 is delivered directly in your private chat. It's the cleanest, safest YouTube to MP3 experience available.</p>
          <p>You can also <a href="/download-spotify-playlist">download full playlists as ZIP</a> with a single message.</p>

          <h2>Frequently Asked Questions</h2>
          <h3>Can I convert YouTube videos to MP3?</h3>
          <p>Yes! TuneDrop converts YouTube Music links to high-quality 320kbps MP3 files. Just send the link to the bot on Telegram and get your MP3 in seconds.</p>
          <h3>Is this YouTube to MP3 converter free?</h3>
          <p>Completely free with no limits. Download as many YouTube videos to MP3 as you want. No premium tier, subscription, or daily cap required.</p>
          <h3>Do I need to install anything?</h3>
          <p>No. TuneDrop runs inside Telegram. If you have Telegram installed, you already have everything you need to convert YouTube to MP3. No extra apps.</p>
          <h3>What about regular YouTube videos (not YouTube Music)?</h3>
          <p>TuneDrop works with YouTube Music links. For best results, use links from music.youtube.com or the YouTube Music app. Regular YouTube video links may also work depending on the content.</p>
          <h3>Can I search for songs without a link?</h3>
          <p>Yes! Use the <code>/song</code> command followed by the song name. For example, <code>/song Shape of You</code> will find and download the track automatically.</p>
        """,
        "schema_extra": """
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://tdrp.cc/"},
              {"@type": "ListItem", "position": 2, "name": "YouTube to MP3", "item": "https://tdrp.cc/youtube-to-mp3"}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
              {"@type": "Question", "name": "Can I convert YouTube videos to MP3?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! TuneDrop converts YouTube Music links to 320kbps MP3 files. Just send the link to the bot on Telegram."}},
              {"@type": "Question", "name": "Is this YouTube to MP3 converter free?", "acceptedAnswer": {"@type": "Answer", "text": "Completely free with no limits. Download as many YouTube videos to MP3 as you want. No premium tier required."}},
              {"@type": "Question", "name": "Do I need to install anything?", "acceptedAnswer": {"@type": "Answer", "text": "No. TuneDrop runs inside Telegram. If you have Telegram, you can convert YouTube to MP3 immediately."}},
              {"@type": "Question", "name": "Can I search for songs without a link?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! Use the /song command followed by the song name to search and download automatically."}}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "name": "How to Convert YouTube to MP3",
            "description": "Convert any YouTube Music video to MP3 using TuneDrop on Telegram.",
            "step": [
              {"@type": "HowToStep", "name": "Open TuneDrop", "text": "Search for @TuneDropOfficialBot on Telegram and press Start."},
              {"@type": "HowToStep", "name": "Paste YouTube Link", "text": "Copy any YouTube Music video or playlist URL and paste it in the chat."},
              {"@type": "HowToStep", "name": "Download MP3", "text": "Receive a 320kbps MP3 file with cover art delivered instantly."}
            ]
          }
          </script>
        """,
    },
    "/telegram-music-bot": {
        "page_title": "Telegram Music Bot — Free MP3 Downloader | TuneDrop",
        "page_desc": "The best free Telegram music bot. Download Spotify & YouTube songs as 320kbps MP3 instantly. No signup, playlists as ZIP. Try TuneDrop.",
        "h1_title": "Telegram Music Bot — <span class='gradient-text'>Free MP3 Downloader</span>",
        "h1_sub": "The best free Telegram bot for downloading music from Spotify and YouTube. 320kbps MP3, no login, instant delivery.",
        "content": """
          <h2>The Best Free Telegram Music Bot</h2>
          <p>TuneDrop is a free <strong>Telegram music bot</strong> that lets you download any song from Spotify or YouTube Music as a high-quality 320kbps MP3 file. No accounts, no subscriptions, no software installations — just open Telegram, send a link, and get your music delivered directly in your chat.</p>
          <p>Telegram bots are the fastest way to get things done inside your favorite messaging app. With over 800 million Telegram users worldwide, a Telegram music bot is the most accessible way to download music — no website to visit, no app to install, no ads to close. Just send a message and get your song.</p>

          <h2>How to Use the TuneDrop Telegram Music Bot</h2>
          <ol>
            <li><strong>Open @TuneDropOfficialBot</strong> on Telegram — tap this link or search in Telegram</li>
            <li><strong>Send a Spotify or YouTube Music link</strong> — or use <code>/song</code> + track name to search</li>
            <li><strong>Receive your MP3</strong> — 320kbps with cover art, delivered in seconds</li>
          </ol>
          <p>That's the entire process. No settings to configure, no commands to memorize, no sign-up forms. Just send a link and get music. You can also manage your downloads with the <code>/myfiles</code> command to see all your recent files.</p>

          <h2>TuneDrop Bot Commands</h2>
          <ul>
            <li><code>/song &lt;name&gt;</code> — Search and download a song by name (e.g., <code>/song Someone Like You</code>)</li>
            <li><code>/myfiles</code> — View your recent downloads with download links and revoke options</li>
            <li><code>/cancel</code> — Stop a current download task</li>
            <li><code>/donation</code> — Support TuneDrop with optional Telegram Stars donations</li>
            <li><strong>Or just send any Spotify / YouTube Music link</strong> — no command needed</li>
          </ul>

          <h2>Why TuneDrop Is the Best Telegram Music Bot</h2>
          <ul>
            <li><strong>Two platforms</strong> — <a href="/spotify-to-mp3">Spotify to MP3</a> and <a href="/youtube-to-mp3">YouTube to MP3</a> in one bot</li>
            <li><strong>320kbps quality</strong> — highest standard MP3 audio, not compressed 128kbps</li>
            <li><strong>Smart caching</strong> — songs you've downloaded before are served instantly on repeat requests</li>
            <li><strong>Playlist support</strong> — <a href="/download-spotify-playlist">full playlists delivered as ZIP files</a></li>
            <li><strong>Full metadata</strong> — cover art, artist info, album name, and ID3 tags included</li>
            <li><strong>File management</strong> — <code>/myfiles</code> lets you browse, download, and revoke past files</li>
            <li><strong>Completely free</strong> — no limits, no premium tier, no daily caps</li>
            <li><strong>Private</strong> — no account needed, no personal data stored or collected</li>
            <li><strong>Concurrent downloads</strong> — playlists are processed in parallel for speed</li>
          </ul>

          <h2>Download Spotify and YouTube Playlists</h2>
          <p>Send any playlist URL from Spotify or YouTube Music and TuneDrop downloads every track, packs them into a ZIP file, and delivers it as a download link. Each song has proper metadata and cover art — your library stays organized automatically. Whether it's a workout playlist, a road trip mix, or your Discover Weekly, TuneDrop handles it.</p>
          <p>See our guides for <a href="/spotify-to-mp3">converting Spotify to MP3</a> and <a href="/youtube-to-mp3">converting YouTube to MP3</a> for platform-specific instructions.</p>

          <h2>Why Use a Telegram Bot for Music Downloads?</h2>
          <p>Telegram bots have major advantages over web-based music downloaders:</p>
          <ul>
            <li><strong>No ads</strong> — zero popups, zero fake download buttons, zero redirects</li>
            <li><strong>No malware risk</strong> — you never visit a website or download an executable</li>
            <li><strong>Private</strong> — downloads happen in your private Telegram chat</li>
            <li><strong>Mobile-first</strong> — designed for phones, not desktop browsers</li>
            <li><strong>Always available</strong> — the bot runs 24/7, no downtime</li>
            <li><strong>File management</strong> — all your downloads organized in one conversation</li>
          </ul>

          <h2>Frequently Asked Questions</h2>
          <h3>Is TuneDrop free?</h3>
          <p>Yes, 100% free with no limits. Download as many songs and playlists as you want. Optional Telegram Stars donations help keep the bot running for everyone.</p>
          <h3>Does TuneDrop work on iPhone and Android?</h3>
          <p>Yes! TuneDrop works on any device with Telegram installed — iOS, Android, Windows, Mac, and Linux. No separate app needed.</p>
          <h3>Do I need a Spotify or YouTube account?</h3>
          <p>No. Just paste the link. TuneDrop doesn't require any account or login from you. Any public link works.</p>
          <h3>How many songs can I download?</h3>
          <p>There's no limit. Download as many individual songs and playlists as you want. The service is completely free with no daily or monthly caps.</p>
          <h3>What about copyright?</h3>
          <p>TuneDrop is a tool for personal use. Users should respect copyright laws in their jurisdiction and only download music they have the right to access.</p>
        """,
        "schema_extra": """
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://tdrp.cc/"},
              {"@type": "ListItem", "position": 2, "name": "Telegram Music Bot", "item": "https://tdrp.cc/telegram-music-bot"}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
              {"@type": "Question", "name": "Is TuneDrop free?", "acceptedAnswer": {"@type": "Answer", "text": "Yes, 100% free with no limits. Download unlimited songs and playlists. Optional Stars donations support the service."}},
              {"@type": "Question", "name": "Does TuneDrop work on iPhone and Android?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! Works on any device with Telegram — iOS, Android, Windows, Mac, Linux."}},
              {"@type": "Question", "name": "Do I need a Spotify or YouTube account?", "acceptedAnswer": {"@type": "Answer", "text": "No. Just paste any public Spotify or YouTube Music link. No login or account needed."}},
              {"@type": "Question", "name": "How many songs can I download?", "acceptedAnswer": {"@type": "Answer", "text": "No limit. Download as many songs and playlists as you want — completely free."}}
            ]
          }
          </script>
        """,
    },
    "/download-spotify-playlist": {
        "page_title": "Download Spotify Playlist to MP3 — Free ZIP | TuneDrop",
        "page_desc": "Download full Spotify playlists as MP3 ZIP files. Free, instant, 320kbps quality. No login required. Try TuneDrop's playlist downloader.",
        "h1_title": "Download Spotify Playlist — <span class='gradient-text'>Free MP3 ZIP</span>",
        "h1_sub": "Convert entire Spotify playlists to MP3 files packed in a ZIP. Free, instant, 320kbps quality. No login required.",
        "content": """
          <h2>How to Download a Spotify Playlist to MP3</h2>
          <p>Downloading a Spotify playlist to MP3 has never been easier. With TuneDrop, you just send the playlist URL to our <a href="/telegram-music-bot">Telegram music bot</a> and receive a ZIP file containing all tracks as high-quality 320kbps MP3 files. Every song includes cover art, artist information, and properly formatted ID3 tags.</p>
          <ol>
            <li><strong>Copy your Spotify playlist link</strong> — open Spotify, tap Share, and copy the link</li>
            <li><strong>Send it to @TuneDropOfficialBot</strong> on Telegram</li>
            <li><strong>Get your ZIP download</strong> — all tracks as 320kbps MP3 with metadata, packed in one file</li>
          </ol>

          <h2>Why Download Spotify Playlists as MP3?</h2>
          <p>Spotify doesn't allow you to export your music as files. With TuneDrop, you can download your favorite playlists as MP3 files to listen anywhere — offline, in your car, on a plane, or in any media player. No internet connection needed after downloading. Your music, your way.</p>
          <p>This is perfect for situations where you don't have reliable internet: flights, road trips, the gym, or anywhere with poor connectivity. Once downloaded, the MP3 files work with every music player on every device.</p>

          <h2>Features of TuneDrop's Spotify Playlist Downloader</h2>
          <ul>
            <li><strong>All tracks included</strong> — every song in the playlist is downloaded, no skipping</li>
            <li><strong>320kbps MP3</strong> — highest standard audio quality for every single track</li>
            <li><strong>ZIP package</strong> — all songs in one convenient ZIP file, easy to extract</li>
            <li><strong>Full metadata</strong> — cover art, artist, album, track number for every song</li>
            <li><strong>Smart caching</strong> — previously downloaded tracks are served instantly</li>
            <li><strong>Concurrent processing</strong> — multiple tracks downloaded simultaneously for speed</li>
            <li><strong>Real-time progress</strong> — see download progress updates in your Telegram chat</li>
            <li><strong>No size limit</strong> — playlists of any size are supported, from 5 to 500+ tracks</li>
          </ul>

          <h2>Spotify Playlist to MP3 — Any Size</h2>
          <p>Whether your playlist has 5 songs or 500, TuneDrop handles it. The bot processes tracks concurrently for the fastest possible download. You'll see real-time progress updates in your Telegram chat as each track is downloaded and added to the ZIP. Large playlists are handled with the same quality and care as small ones.</p>

          <h2>YouTube Music Playlists Too</h2>
          <p>TuneDrop also supports YouTube Music playlists with the same quality and convenience. Send any YouTube Music playlist URL and get the same high-quality ZIP download with all tracks as 320kbps MP3 files. One bot, two platforms, unlimited downloads. Check our <a href="/youtube-to-mp3">YouTube to MP3 converter</a> for more details.</p>

          <h2>TuneDrop vs Other Spotify Playlist Downloaders</h2>
          <p>Most Spotify playlist downloaders are web-based tools full of ads, popups, and fake download buttons. They often limit the number of tracks or require you to create an account. Some even require you to download desktop software that might contain malware.</p>
          <p>TuneDrop runs inside Telegram — no ads, no popups, no software installations, no accounts. Just send a link and get your playlist as a ZIP. It's the safest, cleanest, and fastest way to download Spotify playlists to MP3.</p>
          <p>You can also convert <a href="/spotify-to-mp3">individual Spotify songs to MP3</a> with the same bot.</p>

          <h2>Frequently Asked Questions</h2>
          <h3>How long does it take to download a playlist?</h3>
          <p>Most playlists download in under a minute. Larger playlists take a bit longer, but TuneDrop processes tracks concurrently for maximum speed. You'll see progress updates in real time.</p>
          <h3>Can I download someone else's Spotify playlist?</h3>
          <p>Yes! Any public Spotify playlist can be downloaded. Just copy the share link and send it to TuneDrop. Private playlists can't be accessed.</p>
          <h3>What format are the downloads?</h3>
          <p>All tracks are 320kbps MP3 files packed into a ZIP. Each song has proper file names (Artist - Title.mp3), metadata, and embedded cover art.</p>
          <h3>Is there a playlist size limit?</h3>
          <p>No. TuneDrop supports playlists of any size. Whether it's 10 songs or 500+, every track is downloaded and included in the ZIP.</p>
          <h3>Can I download Spotify albums the same way?</h3>
          <p>Yes! Send any Spotify album link and TuneDrop downloads all tracks. The album tracks come organized with proper track numbers in the filename.</p>
        """,
        "schema_extra": """
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://tdrp.cc/"},
              {"@type": "ListItem", "position": 2, "name": "Download Spotify Playlist", "item": "https://tdrp.cc/download-spotify-playlist"}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
              {"@type": "Question", "name": "How long does it take to download a playlist?", "acceptedAnswer": {"@type": "Answer", "text": "Most playlists download in under a minute. Larger playlists take longer, but TuneDrop processes tracks concurrently for speed."}},
              {"@type": "Question", "name": "Can I download someone else's Spotify playlist?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! Any public Spotify playlist can be downloaded. Just copy the share link and send it to TuneDrop."}},
              {"@type": "Question", "name": "What format are the downloads?", "acceptedAnswer": {"@type": "Answer", "text": "All tracks are 320kbps MP3 files packed into a ZIP with proper filenames, metadata, and embedded cover art."}},
              {"@type": "Question", "name": "Is there a playlist size limit?", "acceptedAnswer": {"@type": "Answer", "text": "No. TuneDrop supports playlists of any size — 10 songs or 500+, every track is downloaded."}},
              {"@type": "Question", "name": "Can I download Spotify albums the same way?", "acceptedAnswer": {"@type": "Answer", "text": "Yes! Send any Spotify album link and all tracks are downloaded with proper track numbers."}}
            ]
          }
          </script>
          <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "name": "How to Download a Spotify Playlist to MP3",
            "description": "Download any Spotify playlist as a ZIP of 320kbps MP3 files using TuneDrop on Telegram.",
            "step": [
              {"@type": "HowToStep", "name": "Copy Playlist Link", "text": "Open Spotify, navigate to your playlist, tap Share, and copy the link."},
              {"@type": "HowToStep", "name": "Send to TuneDrop", "text": "Paste the link in the @TuneDropOfficialBot chat on Telegram."},
              {"@type": "HowToStep", "name": "Download ZIP", "text": "Receive a ZIP file containing all tracks as 320kbps MP3 with cover art and metadata."}
            ]
          }
          </script>
        """,
    },
}

_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_token(token: str) -> None:
    if not token or len(token) > 64 or not _SAFE_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail="Invalid token format")


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_web_app() -> FastAPI:
    app = FastAPI(title="Telegram Music Downloader")

    # Pure ASGI middleware — avoids BaseHTTPMiddleware bugs with StreamingResponse
    _allowed_domain = settings.download_base_url.replace("https://", "").replace("http://", "").split(":")[0]
    _SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://i.ytimg.com; "
            "frame-src https://cardinaltangible.com; "
            "connect-src 'self'"
        ),
    }

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # Block direct IP access — only allow configured domain
        host = request.headers.get("host", "").split(":")[0]
        if host and host != _allowed_domain and host != "localhost" and not host.endswith(f".{_allowed_domain}"):
            return Response(content="Not Found", status_code=404)

        try:
            response = await call_next(request)
        except Exception:
            logger.warning("Downstream handler failed for %s", request.url.path, exc_info=True)
            return Response(content="Internal Server Error", status_code=500)

        for k, v in _SECURITY_HEADERS.items():
            response.headers[k] = v
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # GZip only for text-based content — skip audio/video/zip (already compressed)
    app.add_middleware(GZipMiddleware, minimum_size=500)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> dict[str, Any]:
        """Performance metrics for monitoring."""
        from tunedrop.app.utils.perf import get_perf_stats
        from tunedrop.app.utils.memory_cache import _song_cache, _file_url_cache
        return {
            "perf": get_perf_stats(),
            "memory_cache": {
                "songs": {"size": len(_song_cache)},
                "file_urls": {"size": len(_file_url_cache)},
            },
            "file_url_cache_size": 0,  # deprecated — always uses MTProto now
        }

    @app.get("/robots.txt", response_class=Response)
    async def robots_txt():
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /download/\n"
            "Disallow: /file/\n"
            "Disallow: /generate/\n"
            f"\nSitemap: {settings.download_base_url}/sitemap.xml\n"
        )
        return Response(content=body, media_type="text/plain")

    @app.get("/sitemap.xml", response_class=Response)
    async def sitemap_xml():
        base = settings.download_base_url
        pages = [
            ("/", "1.0"),
            ("/spotify-to-mp3", "0.9"),
            ("/youtube-to-mp3", "0.9"),
            ("/telegram-music-bot", "0.8"),
            ("/download-spotify-playlist", "0.8"),
        ]
        urls = ""
        for path, priority in pages:
            urls += f"""  <url>
    <loc>{base}{path}</loc>
    <priority>{priority}</priority>
    <changefreq>weekly</changefreq>
  </url>\n"""
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}</urlset>"""
        return Response(content=body, media_type="application/xml")

    async def _render_seo_page(request: Request, page_key: str):
        page = _SEO_PAGES.get(page_key)
        if not page:
            raise HTTPException(status_code=404, detail="Page not found")
        bot_link = f"https://t.me/{settings.bot_username}" if settings.bot_username else "#"
        context = {
            "request": request,
            "bot_link": bot_link,
            "site_url": settings.download_base_url,
            "page_path": page_key,
            "page_title": page["page_title"],
            "page_desc": page["page_desc"],
            "h1_title": page["h1_title"],
            "h1_sub": page["h1_sub"],
            "content": page["content"],
            "schema_extra": page.get("schema_extra", ""),
        }
        return templates.TemplateResponse("seo_page.html", context)

    @app.get("/spotify-to-mp3", response_class=HTMLResponse)
    async def seo_spotify_to_mp3(request: Request):
        return await _render_seo_page(request, "/spotify-to-mp3")

    @app.get("/youtube-to-mp3", response_class=HTMLResponse)
    async def seo_youtube_to_mp3(request: Request):
        return await _render_seo_page(request, "/youtube-to-mp3")

    @app.get("/telegram-music-bot", response_class=HTMLResponse)
    async def seo_telegram_music_bot(request: Request):
        return await _render_seo_page(request, "/telegram-music-bot")

    @app.get("/download-spotify-playlist", response_class=HTMLResponse)
    async def seo_download_spotify_playlist(request: Request):
        return await _render_seo_page(request, "/download-spotify-playlist")

    @app.get("/", response_class=HTMLResponse)
    async def landing_page(request: Request):
        bot_link = f"https://t.me/{settings.bot_username}" if settings.bot_username else "#"
        return templates.TemplateResponse("landing.html", {"request": request, "bot_link": bot_link, "site_url": settings.download_base_url})

    @app.get("/generate/{ref}")
    async def generate_download_link(ref: str):
        _validate_token(ref)
        link = await link_store.resolve_ref(ref)
        if not link:
            raise HTTPException(status_code=404, detail="Download reference not found")
        return RedirectResponse(url=link, status_code=307)

    @app.get("/download/{token}", response_class=HTMLResponse)
    async def download_page(request: Request, token: str):
        _validate_token(token)
        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")

        if item.get("expired"):
            context = {
                "request": request,
                "file_name": item.get("file_name", "Unknown"),
                "expired": True,
                "bot_username": settings.bot_username,
                "ads_enabled": settings.ads_enabled,
                "ads_desktop_top_banner": settings.ads_desktop_top_banner,
                "ads_desktop_inline_banner": settings.ads_desktop_inline_banner,
                "ads_mobile_top_banner": settings.ads_mobile_top_banner,
                "ads_mobile_bottom_banner": settings.ads_mobile_bottom_banner,
                "ads_smartlink_url": settings.ads_smartlink_url,
            }
            return templates.TemplateResponse("download.html", context)

        context = {
            "request": request,
            "file_name": item.get("file_name", "Unknown"),
            "parent_token": token,
            "bot_username": settings.bot_username,
            "ads_enabled": settings.ads_enabled,
            "ads_desktop_top_banner": settings.ads_desktop_top_banner,
            "ads_desktop_inline_banner": settings.ads_desktop_inline_banner,
            "ads_mobile_top_banner": settings.ads_mobile_top_banner,
            "ads_mobile_bottom_banner": settings.ads_mobile_bottom_banner,
            "ads_smartlink_url": settings.ads_smartlink_url,
        }
        return templates.TemplateResponse("download.html", context)

    @app.get("/file/{token}")
    async def direct_file(request: Request, token: str):
        _validate_token(token)

        item = await link_store.get(token)
        if not item:
            raise HTTPException(status_code=404, detail="File not found")
        if item.get("expired"):
            raise HTTPException(status_code=410, detail="Download link has expired")

        file_id = item.get("file_id")
        file_name = item.get("file_name", "download.zip")
        file_size = item.get("file_size", 0)

        if not file_id:
            raise HTTPException(status_code=404, detail="File data incomplete")

        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        content_type = {
            "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
            "flac": "audio/flac", "zip": "application/zip",
        }.get(ext, "application/octet-stream")

        # Handle Range requests for resumable downloads
        range_header = request.headers.get("range")
        from_bytes = 0
        until_bytes = max(file_size - 1, 0) if file_size else 0

        if range_header and file_size:
            parsed = _parse_range(range_header, file_size)
            if parsed is False:
                return Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                )
            if parsed is not None:
                from_bytes, until_bytes = parsed

        stream_gen = await mtproto_stream(file_id, from_bytes, until_bytes)
        if stream_gen is None:
            raise HTTPException(status_code=502, detail="Failed to fetch file from Telegram")

        req_length = until_bytes - from_bytes + 1
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file_name, safe=""),
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=300",
            "Content-Encoding": "identity",  # Skip GZip for media files
        }

        if range_header and file_size:
            headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"
            headers["Content-Length"] = str(req_length)
            return StreamingResponse(stream_gen, status_code=206, headers=headers)

        if file_size:
            headers["Content-Length"] = str(file_size)

        return StreamingResponse(stream_gen, headers=headers)

    return app


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None | bool:
    """Parse HTTP Range header. Returns (start, end), None (ignore), or False (invalid)."""
    header = range_header.strip().lower()
    if not header.startswith("bytes="):
        return False
    range_spec = header.split("=", 1)[1].split(",", 1)[0].strip()
    if "-" not in range_spec:
        return False
    start_str, end_str = range_spec.split("-", 1)
    try:
        if start_str == "":
            length = int(end_str)
            if length <= 0:
                return False
            start = max(file_size - length, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
    except ValueError:
        return False
    if start < 0 or end > file_size - 1 or end < start:
        return False
    return start, end


async def _get_media_session(client: Any, file_id_obj: Any) -> Any:
    """Create or reuse a Pyrogram media session for the file's DC."""
    from pyrogram.session import Session, Auth

    media_session = client.media_sessions.get(file_id_obj.dc_id, None)
    if media_session is not None:
        return media_session

    if file_id_obj.dc_id != await client.storage.dc_id():
        media_session = Session(
            client,
            file_id_obj.dc_id,
            await Auth(client, file_id_obj.dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(),
            is_media=True,
        )
        await media_session.start()

        from pyrogram.errors import AuthBytesInvalid
        from pyrogram import raw

        exported_auth = await client.invoke(
            raw.functions.auth.ExportAuthorization(dc_id=file_id_obj.dc_id)
        )
        try:
            await media_session.invoke(
                raw.functions.auth.ImportAuthorization(
                    id=exported_auth.id, bytes=exported_auth.bytes
                )
            )
        except AuthBytesInvalid:
            await media_session.stop()
            raise
    else:
        media_session = Session(
            client,
            file_id_obj.dc_id,
            await client.storage.auth_key(),
            await client.storage.test_mode(),
            is_media=True,
        )
        await media_session.start()

    client.media_sessions[file_id_obj.dc_id] = media_session
    return media_session


async def mtproto_stream(
    file_id: str,
    from_bytes: int = 0,
    until_bytes: int = 0,
) -> AsyncIterator[bytes] | None:
    """Stream a file directly via MTProto upload.GetFile — no temp file needed.

    Decodes the file_id string to get access_hash/file_reference/dc_id,
    creates a media session, and streams chunks via raw GetFile requests.
    Supports up to 4GB. No Bot API 20MB limit.
    """
    from pyrogram.file_id import FileId
    from pyrogram import raw

    client = get_pyrogram_client()
    if client is None:
        logger.warning("Pyrogram client not available for file download")
        return None

    file_id_obj = _fileid_cache.get(file_id)
    if file_id_obj is None:
        try:
            file_id_obj = FileId.decode(file_id)
            _fileid_cache.set(file_id, file_id_obj)
        except Exception:
            logger.exception("Failed to decode file_id: %s", file_id[:40])
            return None

    # Build the InputDocumentFileLocation
    location = raw.types.InputDocumentFileLocation(
        id=file_id_obj.media_id,
        access_hash=file_id_obj.access_hash,
        file_reference=file_id_obj.file_reference,
        thumb_size="",
    )

    try:
        media_session = await _get_media_session(client, file_id_obj)
    except Exception:
        logger.exception("Failed to create media session for DC %s", file_id_obj.dc_id)
        return None

    chunk_size = 1024 * 1024  # 1MB chunks

    # If until_bytes is 0 (unknown size), stream until exhausted
    if until_bytes <= 0:
        _off0 = from_bytes
        async def _stream_unknown() -> AsyncIterator[bytes]:
            _offset = _off0
            try:
                while True:
                    r = await media_session.invoke(
                        raw.functions.upload.GetFile(
                            location=location, offset=_offset, limit=chunk_size,
                        ),
                    )
                    if isinstance(r, raw.types.upload.File):
                        chunk = r.bytes
                        if not chunk:
                            break
                        yield chunk
                        _offset += chunk_size
                    else:
                        break
            except Exception:
                logger.warning("MTProto stream error", exc_info=True)
        return _stream_unknown()

    _off = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - _off
    last_part_cut = until_bytes % chunk_size + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - math.floor(_off / chunk_size)

    async def _stream_range() -> AsyncIterator[bytes]:
        _offset = _off
        current_part = 1
        try:
            r = await media_session.invoke(
                raw.functions.upload.GetFile(
                    location=location, offset=_offset, limit=chunk_size,
                ),
            )
            if not isinstance(r, raw.types.upload.File):
                return
            while True:
                chunk = r.bytes
                if not chunk:
                    break
                if part_count == 1:
                    yield chunk[first_part_cut:last_part_cut]
                elif current_part == 1:
                    yield chunk[first_part_cut:]
                elif current_part == part_count:
                    yield chunk[:last_part_cut]
                else:
                    yield chunk

                current_part += 1
                _offset += chunk_size
                if current_part > part_count:
                    break

                r = await media_session.invoke(
                    raw.functions.upload.GetFile(
                        location=location, offset=_offset, limit=chunk_size,
                    ),
                )
                if not isinstance(r, raw.types.upload.File):
                    break
        except Exception:
            logger.warning("MTProto stream error for file_id=%s", file_id[:40], exc_info=True)

    return _stream_range()
