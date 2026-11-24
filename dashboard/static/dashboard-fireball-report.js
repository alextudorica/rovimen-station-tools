// Fireball report page: "Play all cameras in sync".
//
// Each clip is a 20 s colour chunk that starts at its own UTC second (the
// route puts it in data-start, the event's begin time in the section's
// data-event). Seeking every video to the same UTC moment, a few seconds
// before the event or the latest chunk start if that is later, and starting
// them together shows the fireball on every camera at once.

const PRE_ROLL_S = 3;

function whenEvent(video, name) {
  return new Promise(resolve => {
    const done = () => {
      video.removeEventListener(name, done);
      video.removeEventListener('error', done);
      resolve();
    };
    video.addEventListener(name, done);
    video.addEventListener('error', done);
  });
}

async function playAllInSync(section, button) {
  const videos = [...section.querySelectorAll('video[data-start]')];
  const eventT = Number(section.dataset.event);
  if (videos.length < 2 || !Number.isFinite(eventT)) return;
  const t0 = Math.max(eventT - PRE_ROLL_S, ...videos.map(v => Number(v.dataset.start)));

  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  try {
    videos.forEach(v => v.pause());
    // preload="none" keeps the page light; load metadata only when asked.
    await Promise.all(videos.map(v => {
      if (v.readyState >= 1) return null;
      const ready = whenEvent(v, 'loadedmetadata');
      v.preload = 'auto';
      v.load();
      return ready;
    }));
    await Promise.all(videos.map(v => {
      const target = Math.max(0, t0 - Number(v.dataset.start));
      if (Math.abs(v.currentTime - target) < 0.01) return null;
      const seeked = whenEvent(v, 'seeked');
      v.currentTime = target;
      return seeked;
    }));
    await Promise.all(videos.map(v => v.play().catch(() => {})));
  } finally {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

const section = document.querySelector('.fr-clips[data-event]');
const button = section?.querySelector('.fr-play-all');
button?.addEventListener('click', () => playAllInSync(section, button));
