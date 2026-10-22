import { createParticleScene } from './particles.js';


const logo = document.getElementById('welcome-logo');
const canvas = document.getElementById('welcome-canvas');
const fallback = document.getElementById('welcome-fallback');
const gsap = globalThis.gsap;


gsap.ticker.lagSmoothing(0);
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const state = { time: 0, formation: 0, assembly: 0, settle: 0 };

const startAt = 0.2;
let scene = null;
let timeline = null;
let startFrame = null;
let resizeObserver = null;
let intersectionObserver = null;
let dismissed = false;
let onScreen = true;
let complete = false;

function render() {
  if (dismissed || document.hidden || !onScreen) return;
  scene?.render(state);
  if (timeline) canvas.dataset.time = timeline.time().toFixed(2);
}

function settle() {
  render();
  complete = true;
  gsap.ticker.remove(render);
  logo.classList.add('is-settled');
  logo.dataset.state = 'settled';
}

function showStill() {
  timeline?.pause().progress(1);
  Object.assign(state, { time: 5.2 * 14 / 12, formation: 1, assembly: 1, settle: 1 });
  render();
  settle();
}

function dismiss() {
  if (dismissed) return;
  dismissed = true;
  timeline?.kill();
  if (startFrame !== null) cancelAnimationFrame(startFrame);
  gsap.ticker.remove(render);
  resizeObserver?.disconnect();
  intersectionObserver?.disconnect();
  scene?.dispose();
  logo.classList.add('is-dismissed');
  logo.dataset.state = 'dismissed';
  document.removeEventListener('visibilitychange', syncPlayback);
  reducedMotion.removeEventListener('change', onMotionChange);
}

function syncPlayback() {
  if (!timeline || dismissed) return;
  if (complete) {
    render();
    return;
  }
  if (document.hidden || !onScreen) {
    timeline.pause();
    gsap.ticker.remove(render);
  } else {
    timeline.resume();
    gsap.ticker.add(render);
  }
}

function onMotionChange() {
  if (reducedMotion.matches && !dismissed) showStill();
}

// Draft edits keep the welcome composition intact. The existing .has-chat
// layout hides it for conversations; visibility observers pause the scene so
// returning to a new chat can reuse the logo without a blank space.
document.addEventListener('visibilitychange', syncPlayback);
window.addEventListener('pagehide', dismiss, { once: true });
reducedMotion.addEventListener('change', onMotionChange);


try {
  if (!dismissed) {
    scene = await createParticleScene({
      canvas,
      referenceUrl: new URL('./assets/g-reference.png', import.meta.url).href,
      onReferenceLoaded({ data, width, height }) {
        // Reuse the recovered alpha for a crisp still when WebGL is unavailable.
        fallback.width = width; fallback.height = height;
        const context = fallback.getContext('2d');
        if (!context) return;
        context.putImageData(new ImageData(new Uint8ClampedArray(data), width, height), 0, 0);
        fallback.dataset.prepared = 'true';
      },
    });
  }

  if (dismissed) {
    scene?.dispose();
  } else {
    let lastWidth = 0, lastHeight = 0;
    const resize = () => {
      const bounds = logo.getBoundingClientRect();


      if (bounds.width === lastWidth && bounds.height === lastHeight) return;
      lastWidth = bounds.width; lastHeight = bounds.height;
      scene.resize(bounds.width, bounds.height);
      render();
    };
    timeline = gsap.timeline({ paused: true, onComplete: settle });
    timeline
      .addLabel('appearance', 0)
      .to(state, { time: 5.2 * 14 / 12, duration: 5.2, ease: 'none' }, 0)
      .to(state, { formation: 1, duration: .4, ease: 'power2.out' }, 0)
      .addLabel('assembly', 1.6)
      .to(state, { assembly: 1, duration: 2.8, ease: 'none' }, 'assembly')
      .addLabel('settling', 3.8)
      .to(state, { settle: 1, duration: .6, ease: 'power3.out' }, 'settling')
      .addLabel('logo', 4.4)
      .addLabel('end', 5.2);
    timeline.seek(startAt, true);


    resize();
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(logo);
    fallback.hidden = true;


    startFrame = requestAnimationFrame(() => {
      startFrame = null;
      if (dismissed) return;
      logo.dataset.state = 'playing';
      if (reducedMotion.matches) showStill();
      else {
        render();
        intersectionObserver = new IntersectionObserver(entries => {
          onScreen = entries[0].isIntersecting;
          syncPlayback();
        });
        intersectionObserver.observe(logo);
        syncPlayback();
      }
    });
  }
} catch {
  scene?.dispose();
  canvas.hidden = true;
  fallback.hidden = fallback.dataset.prepared !== 'true';
  if (!dismissed) {
    logo.dataset.state = 'fallback';
    logo.classList.add('is-settled');
  }
}
