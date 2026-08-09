import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { clearComponentSelection, selectComponent } from './component-selection.js';

// Scene setup
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1a1a2e);

// Camera
const camera = new THREE.PerspectiveCamera(
  50,
  window.innerWidth / window.innerHeight,
  0.1,
  1000
);
camera.position.set(5, 3, 5);

// Renderer
const container = document.getElementById('canvas-container');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(container.clientWidth, container.clientHeight);
renderer.setPixelRatio(window.devicePixelRatio);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1;
container.appendChild(renderer.domElement);

// Environment
const pmremGenerator = new THREE.PMREMGenerator(renderer);
scene.environment = pmremGenerator.fromScene(new RoomEnvironment(), 0.04).texture;

// Controls
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.autoRotate = true;
controls.autoRotateSpeed = 2;

// Lights
const ambientLight = new THREE.AmbientLight(0xffffff, 0.5);
scene.add(ambientLight);

const directionalLight = new THREE.DirectionalLight(0xffffff, 1);
directionalLight.position.set(5, 10, 7);
scene.add(directionalLight);

const directionalLight2 = new THREE.DirectionalLight(0xffffff, 0.5);
directionalLight2.position.set(-5, 5, -5);
scene.add(directionalLight2);

// Grid helper
const gridHelper = new THREE.GridHelper(10, 10, 0x444444, 0x222222);
scene.add(gridHelper);

// Axes helper
const axesHelper = new THREE.AxesHelper(2);
scene.add(axesHelper);

// Animation loop
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

// Resize handler
window.addEventListener('resize', () => {
  camera.aspect = container.clientWidth / container.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(container.clientWidth, container.clientHeight);
});

// Control bindings
const autoRotateCheckbox = document.getElementById('auto-rotate');
autoRotateCheckbox.addEventListener('change', (e) => {
  controls.autoRotate = e.target.checked;
});

// Upload functionality
const uploadBtn = document.getElementById('upload-btn');
const imageInput = document.getElementById('image-input');
const statusBar = document.getElementById('status-bar');
const statusText = document.getElementById('status-text');
const progressFill = document.getElementById('progress-fill');

let currentModelPath = null;
let currentModelRoot = null;

const componentInfo = document.getElementById('component-info');
const componentName = document.getElementById('component-name');
const componentKind = document.getElementById('component-kind');
const componentConfidence = document.getElementById('component-confidence');
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let pointerDownPosition = null;

function showComponentInfo(selection) {
  if (!selection) {
    componentInfo.classList.add('hidden');
    componentName.textContent = '';
    componentKind.textContent = '';
    componentConfidence.textContent = '';
    return;
  }
  componentName.textContent = selection.name || selection.id;
  componentKind.textContent = `Type: ${selection.kind || 'component'}`;
  componentConfidence.textContent = Number.isFinite(selection.confidence)
    ? `Vision confidence: ${(selection.confidence * 100).toFixed(0)}%`
    : 'Vision confidence: unavailable';
  componentInfo.classList.remove('hidden');
}

renderer.domElement.addEventListener('pointerdown', (event) => {
  pointerDownPosition = { x: event.clientX, y: event.clientY };
});

renderer.domElement.addEventListener('pointerup', (event) => {
  if (event.button !== 0 || !pointerDownPosition) return;
  const distance = Math.hypot(
    event.clientX - pointerDownPosition.x,
    event.clientY - pointerDownPosition.y,
  );
  pointerDownPosition = null;
  if (distance > 4) return;

  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const intersections = currentModelRoot
    ? raycaster.intersectObject(currentModelRoot, true)
    : [];
  showComponentInfo(selectComponent(intersections[0]?.object || null));
});

function showStatus(message, progress = null) {
  statusBar.classList.remove('hidden');
  statusText.textContent = message;
  if (progress !== null) {
    progressFill.style.width = `${progress}%`;
  } else {
    progressFill.style.width = '0%';
  }
}

function hideStatus() {
  statusBar.classList.add('hidden');
}

uploadBtn.addEventListener('click', () => {
  imageInput.click();
});

imageInput.addEventListener('change', async (e) => {
  const files = e.target.files;
  const file = files && files[0];
  if (!file) return;

  showStatus('Uploading image...', 0);

  const formData = new FormData();
  formData.append('image', file);

  try {
    const response = await fetch('/api/generate', {
      method: 'POST',
      body: formData
    });

    if (!response.ok) {
      throw new Error(`Upload failed: ${response.statusText}`);
    }

    const result = await response.json();
    const objectName = result.objectName;

    showStatus('Generating 3D model...', 10);
    uploadBtn.disabled = true;
    uploadBtn.textContent = 'Generating...';

    // Poll for status
    await pollForCompletion(objectName);

  } catch (error) {
    console.error('Generation error:', error);
    showStatus(`Error: ${error.message}`);
    uploadBtn.disabled = false;
    uploadBtn.textContent = 'Upload Image';
    setTimeout(hideStatus, 5000);
  }
});

async function pollForCompletion(objectName) {
  const maxAttempts = 120; // 2 minutes max
  let attempts = 0;

  while (attempts < maxAttempts) {
    try {
      const response = await fetch('/api/status');
      const status = await response.json();

      showStatus(status.message || 'Processing...', status.progress || 10);

      if (status.status === 'complete') {
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Upload Image';
        showStatus('Model ready! Loading...', 100);

        currentModelPath = status.modelPath;
        await loadGeneratedModel(status.modelPath, status.modelName);
        setTimeout(hideStatus, 2000);
        return;
      }

      if (status.status === 'error') {
        throw new Error(status.error || 'Generation failed');
      }

      await new Promise(resolve => setTimeout(resolve, 1000));
      attempts++;
    } catch (error) {
      console.error('Status poll error:', error);
      throw error;
    }
  }

  throw new Error('Generation timed out');
}

async function loadGeneratedModel(modelPath, modelName) {
  try {
    // Clear existing models
    window.img2threejsViewer.clearScene();

    // Dynamic import the generated model with cache-busting query so the
    // browser ESM module map doesn't return a stale, deleted file when the
    // server status points to a newer generated path.
    const cacheBustedUrl = `${modelPath}?t=${Date.now()}`;
    console.log('[img2threejs] Loading model:', cacheBustedUrl);
    const module = await import(/* @vite-ignore */ cacheBustedUrl);
    console.log('[img2threejs] Module loaded. Available exports:', Object.keys(module));

    let model;
    // Match generator's pascal_case: "Object_1786013440" -> "Object1786013440"
    const pascalName = modelName
      ? modelName.split(/[^A-Za-z0-9]+/).filter(Boolean)
          .map(p => p[0].toUpperCase() + p.slice(1)).join('')
      : '';
    const createFn =
      module[`create${pascalName}Model`] ||
      module.createThermometerModel ||
      module.createModel ||
      module.default;
    model = createFn && createFn({ textureSize: 1024, qualityPriority: 'reference-fidelity' });

    if (model) {
      window.img2threejsViewer.addModel(model);
      console.log(`Model loaded: ${modelName}`);
    } else {
      console.warn('No model factory found in module', Object.keys(module));
      showStatus('Model generated but loading failed');
    }
  } catch (error) {
    console.error('Failed to load model:', error);
    showStatus(`Error loading model: ${error.message}`);
  }
}

const wireframeCheckbox = document.getElementById('wireframe');
wireframeCheckbox.addEventListener('change', (e) => {
  clearComponentSelection();
  showComponentInfo(null);
  scene.traverse((child) => {
    if (child.isMesh && child.userData.originalMaterials) {
      if (e.target.checked) {
        child.material = child.userData.wireframeMaterial;
      } else {
        child.material = child.userData.originalMaterials;
      }
    }
  });
});

const bgColorPicker = document.getElementById('bg-color');
bgColorPicker.addEventListener('change', (e) => {
  scene.background = new THREE.Color(e.target.value);
});

// Export for external use
window.img2threejsViewer = {
  scene,
  camera,
  renderer,
  controls,
  
  addModel(modelGroup) {
    currentModelRoot = modelGroup;
    scene.add(modelGroup);
    // Auto-fit camera to model
    const box = new THREE.Box3().setFromObject(modelGroup);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);
    camera.position.set(center.x + maxDim * 1.5, center.y + maxDim, center.z + maxDim * 1.5);
    controls.target.copy(center);
    controls.update();
  },
  
  clearScene() {
    clearComponentSelection();
    showComponentInfo(null);
    if (!currentModelRoot) return;

    const geometries = new Set();
    const materials = new Set();
    currentModelRoot.traverse((child) => {
      if (child.geometry) geometries.add(child.geometry);
      const childMaterials = Array.isArray(child.material)
        ? child.material
        : [child.material];
      childMaterials.filter(Boolean).forEach((material) => materials.add(material));
    });
    scene.remove(currentModelRoot);
    geometries.forEach((geometry) => geometry.dispose());
    materials.forEach((material) => material.dispose());
    currentModelRoot = null;
  }
};

// URL param: ?model=createObject_1786100828Model.js
// Lets you open a model directly without going through the upload flow.
//
// Naming contract:
//   - URL param `model` is the **filename** in /src/, e.g.
//     "createObject_1786100828Model.js".
//   - The generated module's factory export name is `createObject<id>Model`
//     (no underscore between "Object" and the id), so we derive that name
//     from the filename and call it directly. We do NOT reuse
//     loadGeneratedModel's pascalName path because that expects the backend
//     modelName ("Object_1786100828") and would strip the underscore.
(async function bootstrapFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const modelFile = params.get('model');
  if (!modelFile) return;

  const safeName = modelFile.replace(/[^A-Za-z0-9_.\-]/g, '');
  if (!safeName.endsWith('.js')) return;
  // Must follow the createObject_<id>Model.js convention.
  const m = safeName.match(/^createObject_(\d+)Model\.js$/);
  if (!m) {
    console.warn('[img2threejs] Unrecognized model filename:', safeName);
    return;
  }
  const id = m[1];
  const factoryName = `createObject${id}Model`;
  const modelPath = `/src/${safeName}`;

  console.log('[img2threejs] Bootstrapping from URL:', safeName, '->', factoryName);

  showStatus('Loading model from URL...', 5);
  try {
    window.img2threejsViewer.clearScene();
    const cacheBustedUrl = `${modelPath}?t=${Date.now()}`;
    const mod = await import(/* @vite-ignore */ cacheBustedUrl);
    const create = mod[factoryName];
    if (typeof create !== 'function') {
      throw new Error(`Module did not export ${factoryName}; got [${Object.keys(mod).join(', ')}]`);
    }
    const model = create({ textureSize: 1024, qualityPriority: 'reference-fidelity' });
    if (model) {
      window.img2threejsViewer.addModel(model);
      console.log(`[img2threejs] Model loaded: ${factoryName}`);
    } else {
      throw new Error('Factory returned null/undefined');
    }
    setTimeout(hideStatus, 1500);
  } catch (err) {
    console.error('[img2threejs] URL bootstrap failed:', err);
    showStatus(`Error: ${err.message}`);
  }
})();

console.log('img2threejs Viewer ready!');
console.log('Use window.img2threejsViewer to interact with the scene.');
