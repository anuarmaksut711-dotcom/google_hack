import * as THREE from './vendor/three.module.js';



const vertexShader =            `
  precision highp float;
  attribute vec3 aTarget;
  attribute vec2 aSourceUv;
  attribute vec4 aSeed;
  uniform float uTime;
  uniform float uFormation;
  uniform float uAssembly;
  uniform float uSettle;
  uniform float uCellSize;
  uniform vec2 uSourceCellSize;
  uniform vec2 uPointer;
  varying vec2 vUv;
  varying vec2 vSourceUv;
  varying float vBrightness;
  varying float vShape;
  varying float vAlpha;
  varying float vDock;

  const float TAU = 6.28318530718;
  const float PI = 3.14159265359;

  mat2 turn(float a) {
    float c = cos(a), s = sin(a);
    return mat2(c, -s, s, c);
  }

  float smoother(float value) {
    float x = clamp(value, 0.0, 1.0);
    return x * x * x * (x * (x * 6.0 - 15.0) + 10.0);
  }



  void funnelAt(float clock, out vec3 origin, out vec3 velocity, out float visibility) {
    float rise = 0.047 + aSeed.z * 0.018;
    float height = fract(aSeed.x + clock * rise);
    float lane = floor(aSeed.y * 5.0);
    float radialScale = 0.68 + 0.35 * aSeed.z;
    float radius = (0.10 + 2.13 * pow(height, 1.08)) * radialScale;
    float radiusRate = 2.13 * 1.08 * pow(max(height, 0.000001), 0.08) * rise * radialScale;
    float wave = height * 20.0 - clock * 1.4 + aSeed.z * TAU;
    float angle = height * 12.8 + lane * TAU / 5.0;
    angle -= clock * (1.18 + 0.22 * (1.0 - height));
    angle += (fract(aSeed.y * 5.0) - 0.5) * 0.55;
    angle += sin(wave) * 0.11;
    float angleRate = rise * 12.8 - (1.18 + 0.22 * (1.0 - height))
      + clock * 0.22 * rise + cos(wave) * 0.11 * (rise * 20.0 - 1.4);
    float c = cos(angle), s = sin(angle);
    float sway = height * 5.0 + clock * 0.42;
    float ripple = angle * 2.0 + aSeed.w * TAU;
    origin = vec3(
      c * radius + sin(sway) * 0.09,
      -2.12 + height * 4.26 + sin(ripple) * 0.035,
      s * radius * 0.77
    );
    velocity = vec3(
      -s * angleRate * radius + c * radiusRate + cos(sway) * 0.09 * (rise * 5.0 + 0.42),
      rise * 4.26 + cos(ripple) * 0.07 * angleRate,
      (c * angleRate * radius + s * radiusRate) * 0.77
    );
    float gathering = 1.0 - uFormation;
    origin.xz *= 1.0 + gathering * 0.12;
    velocity.xz *= 1.0 + gathering * 0.12;
    origin.y += sin(aSeed.y * TAU) * gathering * 0.14;


    visibility = smoothstep(0.0, 0.04, height) * (1.0 - smoothstep(0.94, 1.0, height));
  }

  void main() {
    vUv = uv;

    vSourceUv = aSourceUv + vec2(uv.x - 0.5, 0.5 - uv.y) * uSourceCellSize;
    vShape = aSeed.w;



    float delay = aSeed.z * 0.03;
    float targetPhase = fract((-atan(aTarget.y, aTarget.x) + PI) / TAU);
    float targetRadius = clamp(length(aTarget.xy) / 1.75, 0.0, 1.0);


    float dockingOrder = targetPhase * 0.7 + targetRadius * 0.3;
    float arrival = aSeed.w < 0.9
      ? mix(0.60, 0.7857142857, dockingOrder)
      : mix(0.83, 1.0, (aSeed.w - 0.9) / 0.1);
    float flight = clamp((uAssembly - delay) / (arrival - delay), 0.0, 1.0);
    float progress = smoother(flight);
    float remaining = 1.0 - progress;




    float clockPerAssembly = 2.8 * 14.0 / 12.0;
    float launchClock = uTime - max(0.0, uAssembly - delay) * clockPerAssembly;
    float flightDuration = (arrival - delay) * clockPerAssembly;
    vec3 launch, launchVelocity;
    float launchVisibility;
    funnelAt(launchClock, launch, launchVelocity, launchVisibility);




    float lane = floor(targetPhase * 5.0);
    float along = fract(targetPhase * 5.0);
    float sharedProgress = smoother(uAssembly);
    float spiralAngle = 4.3 * sharedProgress;
    float streamRadius = (0.34 + 2.08 * along + (aSeed.z - 0.5) * 0.024)
      * mix(1.0, 0.76, smoothstep(0.0, 0.8, uAssembly));
    float streamAngle = lane * TAU / 5.0 + 3.65 * along + (aSeed.y - 0.5) * 0.09;
    vec3 stream = vec3(cos(streamAngle) * streamRadius, sin(streamAngle) * streamRadius, launch.y * 0.04);
    stream.xy = turn(spiralAngle) * stream.xy;
    float unfurl = smoother(flight / 0.34);


    float momentum = flightDuration * flight * pow(1.0 - flight, 3.0) * exp(-5.0 * flight);
    vec3 freeCenter = mix(launch, stream, unfurl) + momentum * (1.0 - unfurl) * launchVelocity;
    float dock = smoother((flight - 0.58) / 0.42);
    vec3 center = flight >= 1.0 ? aTarget : mix(freeCenter, aTarget, dock);


    float parallax = remaining * remaining;
    center.xz = turn(uPointer.x * 0.12 * parallax) * center.xz;
    center.yz = turn(uPointer.y * 0.07 * parallax) * center.yz;



    float spinningAngle = aSeed.x * TAU + uTime * (aSeed.z * 2.0 - 1.0);
    float shardAngle = (spinningAngle + spiralAngle * 0.65) * remaining;
    float flyingSize = (1.0 + aSeed.z * 0.30) * (1.0 + aSeed.w * 0.18);
    float chipSize = uCellSize * mix(flyingSize, 1.0, progress);
    chipSize *= 1.0 + (1.0 - uSettle) * progress * remaining * 0.025;
    vec2 chip = position.xy * chipSize;
    chip.x *= mix(1.32, 1.0, progress);
    chip = turn(shardAngle) * chip;

    vec4 viewPosition = modelViewMatrix * vec4(center, 1.0);
    viewPosition.xy += chip;
    gl_Position = projectionMatrix * viewPosition;

    vBrightness = mix(0.91 + aSeed.z * 0.09, 1.0, progress);
    vAlpha = mix(launchVisibility * (0.83 + aSeed.z * 0.17), 1.0, smoothstep(0.0, 0.32, flight));
    vDock = smoothstep(0.65, 1.0, progress);
  }
`;

const fragmentShader =            `
  precision highp float;
  uniform sampler2D uSource;
  varying vec2 vUv;
  varying vec2 vSourceUv;
  varying float vBrightness;
  varying float vShape;
  varying float vAlpha;
  varying float vDock;

  void main() {
    vec2 chip = abs(vUv - 0.5) * 2.0;
    float edge = max(chip.x, chip.y);

    if (vShape > 0.68) edge = max(edge, (chip.x + chip.y) * 0.65);
    float aa = clamp(fwidth(edge) * 0.6, 0.045, 0.36);
    float paperAlpha = 1.0 - smoothstep(1.0 - aa, 1.0, edge);


    vec4 source = texture2D(uSource, vSourceUv);


    float contourAA = max(fwidth(source.a) * 0.65, 0.002);
    float contourAlpha = smoothstep(0.5 - contourAA, 0.5 + contourAA, source.a);
    float sourceAlpha = mix(source.a, contourAlpha, vDock);
    float alpha = sourceAlpha * mix(paperAlpha, 1.0, vDock) * vAlpha;
    if (alpha < 0.001) discard;
    gl_FragColor = vec4(source.rgb * vBrightness, alpha);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

function seededRandom(seed = 0x47a119) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let value = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function recoverSourceAlpha(pixels, width, height) {
  const palette = [[234, 67, 53], [66, 133, 244], [52, 168, 83], [251, 188, 5]];
  const rgba = new Uint8Array(pixels.length);
  const assigned = new Uint8Array(width * height);
  const queue = new Int32Array(width * height);
  let tail = 0;

  for (let index = 0; index < width * height; index += 1) {
    const offset = index * 4;
    let bestError = Infinity, bestCoverage = 1, bestColor = palette[0];


    for (const color of palette) {
      let dot = 0, length = 0;
      for (let channel = 0; channel < 3; channel += 1) {
        const direction = 255 - color[channel];
        dot += (255 - pixels[offset + channel]) * direction;
        length += direction * direction;
      }
      const coverage = Math.max(0, Math.min(1, dot / length));
      let error = 0;
      for (let channel = 0; channel < 3; channel += 1) {
        const predicted = 255 - coverage * (255 - color[channel]);
        error += (pixels[offset + channel] - predicted) ** 2;
      }
      if (error < bestError) {
        bestError = error;
        bestCoverage = coverage;
        bestColor = color;
      }
    }
    const matteEdge = bestCoverage < 0.99999 && bestError <= 100;
    for (let channel = 0; channel < 3; channel += 1) {
      rgba[offset + channel] = matteEdge ? bestColor[channel] : pixels[offset + channel];
    }
    rgba[offset + 3] = Math.round(pixels[offset + 3] * (matteEdge ? bestCoverage : 1));
    if (rgba[offset + 3] > 0) {
      assigned[index] = 1;
      queue[tail++] = index;
    }
  }




  for (let head = 0; head < tail; head += 1) {
    const index = queue[head];
    const x = index % width;
    const neighbors = [];
    if (x > 0) neighbors.push(index - 1);
    if (x + 1 < width) neighbors.push(index + 1);
    if (index >= width) neighbors.push(index - width);
    if (index + width < width * height) neighbors.push(index + width);
    for (const neighbor of neighbors) {
      if (assigned[neighbor]) continue;
      assigned[neighbor] = 1;
      for (let channel = 0; channel < 3; channel += 1) {
        rgba[neighbor * 4 + channel] = rgba[index * 4 + channel];
      }
      queue[tail++] = neighbor;
    }
  }



  const coverage = Uint8Array.from(rgba.filter((_, index) => index % 4 === 3));
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      let sum = 0;
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          sum += coverage[(y + dy) * width + x + dx]
            * (dx === 0 ? 2 : 1) * (dy === 0 ? 2 : 1);
        }
      }
      rgba[(y * width + x) * 4 + 3] = Math.round(sum / 16);
    }
  }
  return rgba;
}

async function sampleReference(referenceUrl) {
  const image = new Image();
  image.crossOrigin = 'anonymous';
  await new Promise((resolve, reject) => {
    image.onload = resolve;
    image.onerror = () => reject(new Error('Не удалось загрузить референс логотипа.'));
    image.src = referenceUrl;
  });

  const samplingCanvas = document.createElement('canvas');
  const downscale = Math.min(1, 640 / Math.max(image.naturalWidth, image.naturalHeight));
  samplingCanvas.width = Math.max(1, Math.round(image.naturalWidth * downscale));
  samplingCanvas.height = Math.max(1, Math.round(image.naturalHeight * downscale));
  const context = samplingCanvas.getContext('2d', { willReadFrequently: true });
  if (!context) throw new Error('Браузер не смог прочитать цвета референса.');
  context.drawImage(image, 0, 0, samplingCanvas.width, samplingCanvas.height);
  const { width, height } = samplingCanvas;
  const pixels = context.getImageData(0, 0, width, height).data;
  const rgba = recoverSourceAlpha(pixels, width, height);



  const colored = (x, y) => {
    const offset = (y * width + x) * 4;
    const r = pixels[offset], g = pixels[offset + 1], b = pixels[offset + 2];
    return pixels[offset + 3] > 160 && Math.max(r, g, b) - Math.min(r, g, b) > 50;
  };

  let minX = width, maxX = 0, minY = height, maxY = 0, coloredCount = 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (!colored(x, y)) continue;
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      coloredCount += 1;
    }
  }
  if (coloredCount < 100) throw new Error('В референсе не найдены цветные частицы логотипа.');

  const random = seededRandom();
  const step = Math.max(1, Math.sqrt(coloredCount / 19600));
  const worldPerPixel = 3.5 / (maxY - minY + 1);


  const centerX = (minX + maxX + 1) / 2;
  const centerY = (minY + maxY + 1) / 2;
  const targets = [], sourceUvs = [], seeds = [];
  for (let top = Math.max(0, minY - 2); top < Math.min(height, maxY + 3); top += step) {
    for (let left = Math.max(0, minX - 2); left < Math.min(width, maxX + 3); left += step) {
      let hasCoverage = false;


      for (let y = Math.max(0, Math.floor(top - 0.5)); y < Math.min(height, Math.ceil(top + step + 0.5)) && !hasCoverage; y += 1) {
        for (let x = Math.max(0, Math.floor(left - 0.5)); x < Math.min(width, Math.ceil(left + step + 0.5)); x += 1) {
          if (rgba[(y * width + x) * 4 + 3] > 0) {
            hasCoverage = true;
            break;
          }
        }
      }
      if (!hasCoverage) continue;
      const column = left + step * 0.5;
      const row = top + step * 0.5;
      targets.push((column - centerX) * worldPerPixel, (centerY - row) * worldPerPixel, 0);
      sourceUvs.push(column / width, row / height);
      seeds.push(random(), random(), random(), random());
    }
  }
  const sourceTexture = new THREE.DataTexture(rgba, width, height, THREE.RGBAFormat, THREE.UnsignedByteType);
  sourceTexture.colorSpace = THREE.SRGBColorSpace;
  sourceTexture.magFilter = THREE.LinearFilter;
  sourceTexture.minFilter = THREE.LinearMipmapLinearFilter;
  sourceTexture.generateMipmaps = true;
  sourceTexture.flipY = false;
  sourceTexture.needsUpdate = true;
  return {
    targets, sourceUvs, seeds, sourceTexture,
    cellSize: step * worldPerPixel,
    sourceCellSize: new THREE.Vector2(step / width, step / height),
  };
}


export async function createParticleScene({ canvas, referenceUrl }) {
  const sampled = await sampleReference(referenceUrl);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  const maxRasterSize = renderer.capabilities.maxTextureSize;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(39, 1, 0.1, 50);
  camera.position.set(0, 0.03, 8);
  camera.lookAt(0, 0.03, 0);

  const base = new THREE.PlaneGeometry(1, 1);
  const geometry = new THREE.InstancedBufferGeometry();
  geometry.setIndex(base.index.clone());
  geometry.setAttribute('position', base.attributes.position.clone());
  geometry.setAttribute('uv', base.attributes.uv.clone());
  geometry.setAttribute('aTarget', new THREE.InstancedBufferAttribute(new Float32Array(sampled.targets), 3));
  geometry.setAttribute('aSourceUv', new THREE.InstancedBufferAttribute(new Float32Array(sampled.sourceUvs), 2));
  geometry.setAttribute('aSeed', new THREE.InstancedBufferAttribute(new Float32Array(sampled.seeds), 4));
  geometry.instanceCount = sampled.targets.length / 3;
  base.dispose();

  const uniforms = {
    uTime: { value: 0 },
    uFormation: { value: 1 },
    uAssembly: { value: 0 },
    uSettle: { value: 0 },
    uCellSize: { value: sampled.cellSize },
    uSourceCellSize: { value: sampled.sourceCellSize },
    uSource: { value: sampled.sourceTexture },
    uPointer: { value: new THREE.Vector2() },
  };
  const material = new THREE.ShaderMaterial({
    uniforms, vertexShader, fragmentShader,
    transparent: true,
    depthTest: true,
    depthWrite: false,
    blending: THREE.NormalBlending,
    side: THREE.DoubleSide,
    toneMapped: false,
  });
  const particles = new THREE.Mesh(geometry, material);

  particles.frustumCulled = false;
  scene.add(particles);
  let disposed = false;

  function resize(width, height) {
    if (disposed) return;
    const safeWidth = Math.max(1, Number(width) || canvas.clientWidth || 1);
    const safeHeight = Math.max(1, Number(height) || canvas.clientHeight || 1);
    camera.aspect = safeWidth / safeHeight;


    const halfFov = THREE.MathUtils.degToRad(camera.fov / 2);
    const fitHeight = 2.6 / Math.tan(halfFov);
    const fitWidth = 2.75 / (Math.tan(halfFov) * camera.aspect);
    camera.position.z = Math.max(8, fitHeight, fitWidth);
    camera.updateProjectionMatrix();


    const presentationScale = document.body.classList.contains('clean-view') ? 2160 / safeHeight : 2;
    const ratio = Math.min(
      Math.max(2, presentationScale, globalThis.devicePixelRatio || 1),
      3840 / safeWidth,
      2160 / safeHeight,
      maxRasterSize / Math.max(safeWidth, safeHeight),
    );
    renderer.setPixelRatio(ratio);
    renderer.setSize(safeWidth, safeHeight, false);
    canvas.dataset.renderWidth = String(canvas.width);
    canvas.dataset.renderHeight = String(canvas.height);
    canvas.dataset.renderScale = ratio.toFixed(3);
  }

  function render({ time = 0, formation = 1, assembly = 0, settle = 0, pointerX = 0, pointerY = 0 } = {}) {
    if (disposed) return;
    uniforms.uTime.value = Number.isFinite(time) ? time : 0;
    uniforms.uFormation.value = THREE.MathUtils.clamp(formation, 0, 1);
    uniforms.uAssembly.value = THREE.MathUtils.clamp(assembly, 0, 1);
    uniforms.uSettle.value = THREE.MathUtils.clamp(settle, 0, 1);
    uniforms.uPointer.value.set(THREE.MathUtils.clamp(pointerX, -1, 1), THREE.MathUtils.clamp(pointerY, -1, 1));
    renderer.render(scene, camera);
  }

  resize(canvas.clientWidth, canvas.clientHeight);
  return {
    render,
    resize,
    count: geometry.instanceCount,
    rendererInfo: renderer.info,
    dispose() {
      if (disposed) return;
      disposed = true;
      scene.remove(particles);
      geometry.dispose();
      material.dispose();
      sampled.sourceTexture.dispose();
      renderer.dispose();
    },
  };
}
