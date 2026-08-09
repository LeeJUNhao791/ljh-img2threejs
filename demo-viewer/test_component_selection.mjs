import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clearComponentSelection,
  findSelectableAncestor,
  selectComponent,
} from './src/component-selection.js';


function fakeMaterial() {
  return {
    disposed: false,
    emissive: {
      value: 0,
      getHex() {
        return this.value;
      },
      setHex(value) {
        this.value = value;
      },
    },
    clone() {
      return fakeMaterial();
    },
    dispose() {
      this.disposed = true;
    },
  };
}


test('finds the nearest sculpt component ancestor', () => {
  const pivot = {
    userData: {
      sculptComponent: { id: 'motor', name: 'Motor', visualKind: 'motor' },
    },
    parent: null,
  };
  const child = { userData: {}, parent: pivot };

  assert.equal(findSelectableAncestor(child), pivot);
});


test('ignores objects without sculpt component metadata', () => {
  assert.equal(findSelectableAncestor({ userData: {}, parent: null }), null);
});


test('selects one component with cloned highlight material and restores it', () => {
  const originalMaterial = fakeMaterial();
  const mesh = {
    isMesh: true,
    material: originalMaterial,
    children: [],
    parent: null,
    userData: {
      sculptComponent: {
        id: 'pump-casing',
        name: 'Pump casing',
        visualKind: 'pump_casing',
        sourceConfidence: 0.92,
      },
    },
  };

  const selection = selectComponent(mesh);

  assert.deepEqual(selection, {
    id: 'pump-casing',
    name: 'Pump casing',
    kind: 'pump_casing',
    confidence: 0.92,
  });
  assert.notEqual(mesh.material, originalMaterial);
  assert.equal(mesh.material.emissive.getHex(), 0x00d9ff);

  const highlightMaterial = mesh.material;
  clearComponentSelection();

  assert.equal(mesh.material, originalMaterial);
  assert.equal(highlightMaterial.disposed, true);
});


test('selecting empty space clears the previous component', () => {
  const originalMaterial = fakeMaterial();
  const mesh = {
    isMesh: true,
    material: originalMaterial,
    children: [],
    parent: null,
    userData: { sculptComponent: { id: 'motor', name: 'Motor', role: 'motor' } },
  };

  selectComponent(mesh);
  assert.equal(selectComponent(null), null);
  assert.equal(mesh.material, originalMaterial);
});
