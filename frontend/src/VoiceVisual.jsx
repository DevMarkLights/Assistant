import { useEffect, useRef } from "react";

// Two looks for the voice-mode animation, chosen in Settings: a ribbon waveform or a glowing orb.
// Each phase sets how it moves; `activity` bursts (one per recognized or spoken word) add energy.

// Wave: wave height, how much activity can add, travel speed, how far the lobes sweep sideways, glow
const WAVE_MOTION = {
  idle: { amp: 0.18, react: 0.5, speed: 0.8, sweep: 0.08, glow: 0.6 },
  listening: { amp: 0.4, react: 1.0, speed: 1.5, sweep: 0.14, glow: 0.9 },
  thinking: { amp: 0.32, react: 0.3, speed: 3.2, sweep: 0.32, glow: 0.85 },
  speaking: { amp: 0.6, react: 1.2, speed: 2.2, sweep: 0.18, glow: 1.0 },
  error: { amp: 0.06, react: 0.2, speed: 0.4, sweep: 0.02, glow: 0.35 },
};

// Orb: base wobble, how much activity can add, swirl speed, speed, glow, how far blobs drift apart
const ORB_MOTION = {
  idle: { amp: 0.07, react: 0.5, spin: 0.2, speed: 0.7, glow: 0.6, drift: 0.6 },
  listening: { amp: 0.11, react: 1.2, spin: 0.4, speed: 1.1, glow: 0.85, drift: 1.0 },
  thinking: { amp: 0.08, react: 0.4, spin: 2.2, speed: 1.8, glow: 0.8, drift: 1.4 },
  speaking: { amp: 0.13, react: 1.6, spin: 0.6, speed: 1.5, glow: 1.0, drift: 1.2 },
  error: { amp: 0.03, react: 0.2, spin: 0.1, speed: 0.4, glow: 0.35, drift: 0.2 },
};

// Ribbons: each is a sine wave squeezed by a bell-shaped envelope and mirrored around the centre
// line, filled translucent and blended additively so crossings glow. Different frequencies,
// speeds and hues keep them out of step with each other.
const RIBBONS = [
  { freq: 1.6, speed: 1.0, phase: 0, hue: 0, width: 0.34, height: 1.0, alpha: 0.55 },
  { freq: 2.3, speed: -1.3, phase: 1.7, hue: 50, width: 0.3, height: 0.8, alpha: 0.5 },
  { freq: 2.9, speed: 1.6, phase: 3.1, hue: -45, width: 0.26, height: 0.7, alpha: 0.5 },
  { freq: 1.2, speed: -0.7, phase: 4.4, hue: 25, width: 0.4, height: 0.55, alpha: 0.4 },
  { freq: 3.6, speed: 2.1, phase: 2.2, hue: -20, width: 0.22, height: 0.5, alpha: 0.45 },
];
const STEPS = 120;

// Orb layers: each is a ring whose radius is a sum of slow sine waves, around a centre that orbits
// the middle, so the layers slide apart and merge
const LAYERS = [
  { scale: 1.0, waves: [[3, 1.0], [5, 0.6]], phase: 0, hue: 0, alpha: 0.5 },
  { scale: 0.85, waves: [[4, 0.9], [2, 0.7]], phase: 2.1, hue: 45, alpha: 0.5 },
  { scale: 0.8, waves: [[5, 0.8], [3, 0.5]], phase: 4.2, hue: -40, alpha: 0.45 },
  { scale: 0.55, waves: [[2, 1.0], [6, 0.4]], phase: 1.3, hue: 20, alpha: 0.6 },
];
const POINTS = 96;
const PARTICLES = 36;

function cssColor(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// Parses "#rrggbb" into HSL so shapes can be tinted around the theme's accent colour
function hexToHsl(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) return [248, 60, 60];
  const [r, g, b] = m.slice(1).map((x) => parseInt(x, 16) / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l * 100];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s * 100, l * 100];
}

function drawWave(ctx, st, width, height, h, s, scale) {
  const cy = height / 2;
  const maxAmp = height * 0.46;
  const level = Math.min(1, st.motion.amp + st.energy * 0.6 * st.motion.react) * scale;
  RIBBONS.forEach((rb, i) => {
    // The bulge of each ribbon drifts left and right on its own rhythm
    const centre = 0.5 + st.motion.sweep * Math.sin(st.t * (0.5 + i * 0.23) + rb.phase) * (i % 2 ? -1 : 1);
    const pulse = 0.75 + 0.25 * Math.sin(st.t * (1.1 + i * 0.4) + rb.phase);
    const amp = maxAmp * level * rb.height * pulse;
    const ys = [];
    for (let p = 0; p <= STEPS; p++) {
      const xn = p / STEPS;
      const env = Math.exp(-(((xn - centre) / rb.width) ** 2));
      ys.push(amp * env * Math.sin(xn * Math.PI * 2 * rb.freq + st.t * rb.speed * 2 + rb.phase));
    }
    ctx.beginPath();
    ys.forEach((y, p) => ctx[p ? "lineTo" : "moveTo"]((p / STEPS) * width, cy - y));
    for (let p = STEPS; p >= 0; p--) ctx.lineTo((p / STEPS) * width, cy + ys[p] * 0.85);
    ctx.closePath();
    const hue = h + rb.hue + 20 * Math.sin(st.t * 0.3 + i);
    const fill = ctx.createLinearGradient(0, 0, width, 0);
    const a = rb.alpha * st.motion.glow;
    fill.addColorStop(0, `hsla(${hue}, ${s}%, 60%, 0)`);
    fill.addColorStop(Math.max(0.05, Math.min(0.95, centre)), `hsla(${hue}, ${Math.min(100, s + 20)}%, 68%, ${a})`);
    fill.addColorStop(1, `hsla(${hue}, ${s}%, 60%, 0)`);
    ctx.fillStyle = fill;
    ctx.fill();
  });

  // A bright core line through the middle, which brightens with energy
  const line = ctx.createLinearGradient(0, 0, width, 0);
  line.addColorStop(0, `hsla(${h}, ${s}%, 85%, 0)`);
  line.addColorStop(0.5, `hsla(${h}, ${s}%, 88%, ${0.35 + 0.4 * Math.min(1, st.energy)})`);
  line.addColorStop(1, `hsla(${h}, ${s}%, 85%, 0)`);
  ctx.fillStyle = line;
  ctx.fillRect(0, cy - 1, width, 2);
}

function drawOrb(ctx, st, width, height, h, s, scale) {
  const cx = width / 2, cy = height / 2;
  const base = Math.min(width, height) * 0.3;
  const amp = (st.motion.amp + st.energy * 0.14 * st.motion.react) * scale;

  // Soft halo behind everything (drawn normally, before switching to additive blending)
  ctx.globalCompositeOperation = "source-over";
  const halo = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, base * 1.9);
  halo.addColorStop(0, `hsla(${h}, ${s}%, 65%, ${0.28 * st.motion.glow + st.energy * 0.12})`);
  halo.addColorStop(1, `hsla(${h}, ${s}%, 65%, 0)`);
  ctx.fillStyle = halo;
  ctx.fillRect(0, 0, width, height);
  ctx.globalCompositeOperation = "lighter";

  LAYERS.forEach((layer, i) => {
    const rot = st.t * st.motion.spin * (i % 2 ? -1 : 1) + layer.phase;
    const r0 = base * layer.scale * (1 + st.energy * 0.1);
    const orbit = base * (0.1 + st.energy * 0.08) * st.motion.drift * (i === 3 ? 0.5 : 1) * scale;
    const oa = st.t * (0.6 + i * 0.35) * (i % 2 ? 1 : -1) + layer.phase;
    const bx = cx + orbit * Math.cos(oa), by = cy + orbit * Math.sin(oa * 1.3);
    ctx.beginPath();
    for (let p = 0; p <= POINTS; p++) {
      const a = (p / POINTS) * Math.PI * 2;
      let wobble = 0;
      for (const [k, w] of layer.waves) wobble += w * Math.sin(k * a + st.t * (1.2 + k * 0.25) + layer.phase);
      const r = r0 * (1 + amp * wobble);
      ctx[p ? "lineTo" : "moveTo"](bx + r * Math.cos(a + rot), by + r * Math.sin(a + rot));
    }
    ctx.closePath();
    const fill = ctx.createRadialGradient(bx - r0 * 0.3, by - r0 * 0.35, r0 * 0.05, bx, by, r0 * 1.15);
    const hue = h + layer.hue + 25 * Math.sin(st.t * 0.4 + i);
    fill.addColorStop(0, `hsla(${hue}, ${Math.min(100, s + 15)}%, 80%, ${layer.alpha * st.motion.glow})`);
    fill.addColorStop(0.6, `hsla(${hue}, ${s}%, 58%, ${layer.alpha * 0.8 * st.motion.glow})`);
    fill.addColorStop(1, `hsla(${hue}, ${s}%, 40%, 0)`);
    ctx.fillStyle = fill;
    ctx.fill();
  });

  // A ring of sparks around the blobs: it swirls with the phase and pulses outward with energy
  for (let p = 0; p < PARTICLES; p++) {
    const a = (p / PARTICLES) * Math.PI * 2 + st.t * st.motion.spin * 0.8;
    const wave = Math.sin(a * 3 + st.t * 2.2) + 0.6 * Math.sin(a * 5 - st.t * 1.7 + p);
    const r = base * (1.35 + 0.1 * wave * (0.4 + st.energy) + st.energy * 0.12);
    const size = 1.2 + 1.4 * (0.5 + 0.5 * Math.sin(st.t * 3 + p * 1.7)) * (0.5 + st.energy);
    ctx.beginPath();
    ctx.arc(cx + r * Math.cos(a), cy + r * Math.sin(a), size, 0, Math.PI * 2);
    ctx.fillStyle = `hsla(${h + 30 * Math.sin(a + st.t)}, ${s}%, 75%, ${(0.25 + 0.35 * st.motion.glow) * (0.5 + 0.5 * Math.sin(st.t * 2 + p))})`;
    ctx.fill();
  }
}

const LOOKS = { wave: { motion: WAVE_MOTION, draw: drawWave }, orb: { motion: ORB_MOTION, draw: drawOrb } };

export default function VoiceVisual({ phase, activity, look = "wave" }) {
  const canvasRef = useRef(null);
  const state = useRef({ energy: 0, motion: null, t: 0 });
  const phaseRef = useRef(phase);

  // The animation loop reads the phase through a ref, so it never restarts
  useEffect(() => {
    phaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    state.current.energy = Math.min(1.6, state.current.energy + 0.55);
  }, [activity]);

  useEffect(() => {
    const { motion: MOTION, draw: drawLook } = LOOKS[look] || LOOKS.wave;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // With Reduce Motion on it still animates (a frozen visual looks broken), just slower and smaller
    const calm = reduced ? 0.5 : 1;
    const [h, s] = hexToHsl(cssColor("--accent", "#8f88e3"));
    const st = state.current;
    st.motion = { ...MOTION.idle };
    let frame;
    let last = performance.now();

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const { width, height } = canvas.getBoundingClientRect();
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    const draw = (now) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const target = MOTION[phaseRef.current] || MOTION.idle;
      // Ease between phases instead of jumping
      for (const key of Object.keys(target)) st.motion[key] += (target[key] - st.motion[key]) * Math.min(1, dt * 3);
      st.energy *= Math.exp(-dt * 2.4);
      if (phaseRef.current === "listening") st.energy = Math.max(st.energy, 0.2 + 0.15 * Math.sin(now / 500)); // breathing
      st.t += dt * st.motion.speed * calm;

      const { width, height } = canvas.getBoundingClientRect();
      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = "lighter";
      drawLook(ctx, st, width, height, h, s, reduced ? 0.7 : 1);
      ctx.globalCompositeOperation = "source-over";
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
    };
  }, [look]);

  return <canvas ref={canvasRef} className="voice-visual" aria-hidden="true" />;
}
