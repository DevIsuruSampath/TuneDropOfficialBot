// ===== Navigation scroll effect =====
const nav = document.getElementById('nav');

function handleNavScroll() {
  nav.classList.toggle('scrolled', window.scrollY > 40);
}

window.addEventListener('scroll', handleNavScroll, { passive: true });
handleNavScroll();

// ===== Mobile menu =====
const navToggle = document.getElementById('navToggle');
const mobileMenu = document.getElementById('mobileMenu');

navToggle.addEventListener('click', () => {
  const isOpen = mobileMenu.classList.toggle('open');
  navToggle.classList.toggle('active', isOpen);
  navToggle.setAttribute('aria-expanded', String(isOpen));
});

// Close mobile menu on link click
mobileMenu.querySelectorAll('a').forEach(link => {
  link.addEventListener('click', () => {
    navToggle.classList.remove('active');
    mobileMenu.classList.remove('open');
    navToggle.setAttribute('aria-expanded', 'false');
  });
});

// Close mobile menu on outside click
document.addEventListener('click', (e) => {
  if (mobileMenu.classList.contains('open') &&
      !mobileMenu.contains(e.target) &&
      !navToggle.contains(e.target)) {
    navToggle.classList.remove('active');
    mobileMenu.classList.remove('open');
    navToggle.setAttribute('aria-expanded', 'false');
  }
});

// ===== Smooth scroll =====
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', (e) => {
    const target = document.querySelector(anchor.getAttribute('href'));
    if (target) {
      e.preventDefault();
      const offset = 80;
      const top = target.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({ top, behavior: 'smooth' });
    }
  });
});

// ===== Intersection Observer for scroll animations =====
const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  },
  { threshold: 0.15, rootMargin: '0px 0px -40px 0px' }
);

document.querySelectorAll('.step, .feature-card').forEach(el => observer.observe(el));

// Stagger feature card animations
document.querySelectorAll('.feature-card').forEach((card, i) => {
  card.style.transitionDelay = `${i * 0.1}s`;
});

document.querySelectorAll('.step').forEach((step, i) => {
  step.style.transitionDelay = `${i * 0.15}s`;
});

// ===== Phone Mockup Chat Animation =====
const chatBody = document.getElementById('chatBody');

const CHAT_STATES = [
  // State 0: User sends /song command
  {
    render() {
      chatBody.innerHTML = '';
      addMsg('user', '/song Blinding Lights');
    },
    delay: 1200,
  },
  // State 1: Typing indicator
  {
    render() {
      addTyping();
    },
    delay: 1500,
  },
  // State 2: Bot sends song card
  {
    render() {
      removeTyping();
      addSongCard('Blinding Lights', 'The Weeknd', '3:20', '320kbps');
    },
    delay: 2500,
  },
  // State 3: User sends playlist URL
  {
    render() {
      addMsg('user', 'open.spotify.com/playlist/37i9dQZF1DX7Jl5...');
    },
    delay: 1200,
  },
  // State 4: Typing indicator
  {
    render() {
      addTyping();
    },
    delay: 1500,
  },
  // State 5: Playlist progress at 0%
  {
    render() {
      removeTyping();
      addPlaylistCard(0);
    },
    delay: 300,
  },
  // State 6: Progress at 42%
  {
    render() {
      updateProgress(42);
    },
    delay: 1400,
  },
  // State 7: Progress at 100% → ready
  {
    render() {
      updateProgress(100);
      setTimeout(() => showPlaylistReady(), 600);
    },
    delay: 3000,
  },
];

let currentState = -1;

function addMsg(type, text) {
  const el = document.createElement('div');
  el.className = `chat-msg ${type}`;
  el.textContent = text;
  chatBody.appendChild(el);
}

function addTyping() {
  const el = document.createElement('div');
  el.className = 'typing-indicator';
  el.id = 'typingIndicator';
  el.innerHTML = '<span></span><span></span><span></span>';
  chatBody.appendChild(el);
}

function removeTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function addSongCard(name, artist, duration, quality) {
  const el = document.createElement('div');
  el.className = 'song-card';
  el.innerHTML = `
    <div class="song-card-inner">
      <div class="song-cover">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="18" cy="16" r="3" fill="none" stroke="currentColor" stroke-width="2"/></svg>
      </div>
      <div class="song-meta">
        <div class="song-name">${name}</div>
        <div class="song-artist">${artist}</div>
        <div class="song-tags">
          <span class="song-tag quality">${quality}</span>
          <span class="song-tag duration">${duration}</span>
        </div>
      </div>
    </div>
  `;
  chatBody.appendChild(el);
}

function addPlaylistCard(initialPercent) {
  const el = document.createElement('div');
  el.className = 'playlist-card';
  el.id = 'playlistCard';
  el.innerHTML = `
    <div class="playlist-card-title">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Downloading playlist...
    </div>
    <div class="progress-bar-bg">
      <div class="progress-bar-fill" id="progressFill" style="transform: scaleX(${initialPercent / 100})"></div>
    </div>
    <div class="progress-text" id="progressText">${initialPercent}% complete</div>
  `;
  chatBody.appendChild(el);
}

function updateProgress(percent) {
  const fill = document.getElementById('progressFill');
  const text = document.getElementById('progressText');
  if (fill) fill.style.transform = `scaleX(${percent / 100})`;
  if (text) text.textContent = `${percent}% complete`;
}

function showPlaylistReady() {
  const card = document.getElementById('playlistCard');
  if (!card) return;
  card.innerHTML = `
    <div class="playlist-ready-badge">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>
      Playlist Ready
    </div>
    <div class="playlist-stats">
      <span class="playlist-stat"><strong>64</strong> tracks</span>
      <span class="playlist-stat"><strong>361</strong> MB</span>
      <span class="playlist-stat"><strong>41</strong> cached</span>
    </div>
  `;
  card.style.borderColor = 'rgba(74, 222, 128, 0.25)';
}

let chatTimeout = null;

function scheduleChatState() {
  const state = CHAT_STATES[currentState];
  chatTimeout = setTimeout(() => {
    nextChatState();
  }, state.delay);
}

function nextChatState() {
  currentState = (currentState + 1) % CHAT_STATES.length;
  const state = CHAT_STATES[currentState];
  state.render();
  if (document.hidden) {
    chatTimeout = null;
    return;
  }
  scheduleChatState();
}

// Pause/resume animation on tab visibility
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && chatTimeout === null) {
    scheduleChatState();
  }
});

nextChatState();
