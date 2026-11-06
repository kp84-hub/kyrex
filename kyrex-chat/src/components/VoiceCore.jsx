import React, { useEffect, useRef } from 'react';

// Fixed geometry, generated once. Rendering is local presentation only.
const TAU = Math.PI * 2;
const points = Array.from({ length: 170 }, (_, index) => {
  const y = 1 - 2 * (index + .5) / 170, radius = Math.sqrt(1 - y * y);
  const angle = index * Math.PI * (3 - Math.sqrt(5));
  return [Math.cos(angle) * radius, y, Math.sin(angle) * radius];
});
const links = points.flatMap((point, index) => points.slice(index + 1).flatMap((other, offset) =>
  point.reduce((sum, value, axis) => sum + (value - other[axis]) ** 2, 0) < .145 ? [[index, index + offset + 1]] : []));

function paint(context, time, speaking, input) {
  const size = 240, center = size / 2;
  const energy = speaking ? .6 + .18 * Math.sin(time * 4.2) + .1 * Math.sin(time * 7.1) : input * .3;
  const radius = 73 + energy * 6, turn = .5 + time * .45;
  context.clearRect(0, 0, size, size);
  const glow = context.createRadialGradient(center, center, 0, center, center, 106);
  glow.addColorStop(0, `rgba(110,241,255,${.1 + energy * .16})`);
  glow.addColorStop(.55, 'rgba(25,161,255,.06)'); glow.addColorStop(1, 'rgba(10,80,200,0)');
  context.fillStyle = glow; context.fillRect(0, 0, size, size);

  const projected = points.map(([x, y, z], index) => {
    const distortion = 1 + .06 * Math.sin(index * 2.3 + time * 2.8) + energy * .07 * Math.sin(index * .7 - time * 3.2);
    const rx = x * Math.cos(turn) + z * Math.sin(turn), rz = z * Math.cos(turn) - x * Math.sin(turn);
    const ry = y * Math.cos(.32) - rz * Math.sin(.32), depth = rz * Math.cos(.32) + y * Math.sin(.32);
    const scale = 1 / (1 - depth * .16);
    return [center + rx * radius * distortion * scale, center + ry * radius * distortion * scale, depth];
  });
  context.lineWidth = .65;
  for (let layer = 0; layer < 4; layer++) {
    context.beginPath();
    for (const [a, b] of links) {
      const start = projected[a], end = projected[b];
      if (Math.min(3, Math.floor(((start[2] + end[2]) / 2 + 1) * 2)) !== layer) continue;
      context.moveTo(start[0], start[1]); context.lineTo(end[0], end[1]);
    }
    context.strokeStyle = `rgba(${layer > 1 ? '117,237,255' : '36,139,214'},${.1 + layer * .14 + energy * .09})`;
    context.shadowColor = '#65eaff'; context.shadowBlur = layer === 3 ? 5 + energy * 4 : 0;
    context.stroke();
  }
  context.shadowBlur = 0;
  // Interlaced luminous ribbons give the sphere its irregular energy shell.
  for (let ribbon = 0; ribbon < 5; ribbon++) {
    context.beginPath();
    for (let step = 0; step <= 84; step++) {
      const angle = step / 84 * TAU, tilt = ribbon * .63 + .28;
      const wave = radius * (1 + .055 * Math.sin(angle * 6 + ribbon + time * 2.5));
      const x = Math.cos(angle) * wave, y = Math.sin(angle) * wave;
      const px = x * Math.cos(tilt) - y * .38 * Math.sin(tilt);
      const py = x * Math.sin(tilt) + y * .38 * Math.cos(tilt);
      if (step === 0) context.moveTo(center + px, center + py);
      else context.lineTo(center + px, center + py);
    }
    context.strokeStyle = `rgba(119,244,255,${.16 + energy * .2})`; context.lineWidth = .8;
    context.shadowColor = '#43dfff'; context.shadowBlur = 5; context.stroke();
  }
  context.shadowBlur = 0;
  context.fillStyle = '#b5f7ff';
  context.beginPath();
  for (let index = 0; index < projected.length; index++) {
    const [x, y, depth] = projected[index];
    if (depth < -.15) continue;
    const dot = .6 + depth * .5 + energy * .35;
    context.moveTo(x + dot, y); context.arc(x, y, dot, 0, TAU);
  }
  context.fill();
  // Sparse satellites and segmented instrument rings, rather than a solid ball.
  for (let index = 0; index < 26; index++) {
    const angle = index * 2.4 + time * (index % 2 ? -.17 : .22);
    const orbit = 93 + (index % 5) * 3;
    context.fillStyle = `rgba(100,223,255,${.25 + (index % 4) * .1})`;
    context.beginPath(); context.arc(center + Math.cos(angle) * orbit, center + Math.sin(angle) * orbit * .9, index % 4 ? .65 : 1.3, 0, TAU); context.fill();
  }
  context.strokeStyle = 'rgba(65,185,242,.32)'; context.lineWidth = .8;
  context.setLineDash([2, 9]); context.beginPath(); context.arc(center, center, 109, 0, TAU); context.stroke(); context.setLineDash([]);
  context.strokeStyle = `rgba(129,244,255,${.35 + energy * .25})`; context.lineWidth = 1.2;
  for (let arc = 0; arc < 3; arc++) {
    const angle = arc / 3 * TAU - time * .3;
    context.beginPath(); context.arc(center, center, 102, angle, angle + .44); context.stroke();
  }
  const core = context.createRadialGradient(center, center, 0, center, center, 25 + energy * 14);
  core.addColorStop(0, `rgba(215,255,255,${.55 + energy * .25})`);
  core.addColorStop(.2, 'rgba(89,226,255,.28)'); core.addColorStop(1, 'rgba(20,160,255,0)');
  context.fillStyle = core; context.fillRect(0, 0, size, size);
}

export default function VoiceCore({ phase, level = 0 }) {
  const canvas = useRef(null), input = useRef(level), refresh = useRef(null);
  input.current = level;
  useEffect(() => {
    if (!canvas.current || typeof window.CanvasRenderingContext2D === 'undefined') return undefined;
    const context = canvas.current.getContext('2d');
    if (!context) return undefined;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.current.width = canvas.current.height = 240 * ratio; context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, start = 0, closed = false;
    const animated = () => phase === 'speaking' && !motion.matches && !document.hidden;
    const draw = timestamp => {
      if (closed || !animated()) return;
      if (timestamp - last >= 1000 / 30) { paint(context, (timestamp - start) / 1000, true, 0); last = timestamp; }
      frame = requestAnimationFrame(draw);
    };
    const update = () => {
      cancelAnimationFrame(frame);
      paint(context, 0, false, motion.matches ? 0 : Math.min(1, Math.max(0, input.current / 100)));
      if (animated()) { start = performance.now(); last = 0; frame = requestAnimationFrame(draw); }
    };
    refresh.current = () => { if (!animated() && !closed) paint(context, 0, false, motion.matches ? 0 : Math.min(1, Math.max(0, input.current / 100))); };
    update(); motion.addEventListener('change', update); document.addEventListener('visibilitychange', update);
    return () => { closed = true; cancelAnimationFrame(frame); refresh.current = null; motion.removeEventListener('change', update); document.removeEventListener('visibilitychange', update); };
  }, [phase]);
  useEffect(() => { refresh.current?.(); }, [level]);
  return <div className="voice-core" data-speaking={phase === 'speaking' || undefined} aria-hidden="true"><div className="voice-core-halo" /><canvas ref={canvas} className="voice-core-canvas" /></div>;
}
