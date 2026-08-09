let selectedMaterialEntries = [];


export function findSelectableAncestor(object) {
  let current = object || null;
  while (current) {
    if (current.userData && current.userData.sculptComponent) {
      return current;
    }
    current = current.parent || null;
  }
  return null;
}


function cloneHighlightedMaterial(material) {
  if (!material || typeof material.clone !== 'function') {
    return material;
  }
  const highlighted = material.clone();
  if (highlighted.emissive && typeof highlighted.emissive.setHex === 'function') {
    highlighted.emissive.setHex(0x00d9ff);
  }
  return highlighted;
}


function meshesWithin(root) {
  const meshes = [];
  const pending = [root];
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current) continue;
    if (current.isMesh) meshes.push(current);
    if (Array.isArray(current.children)) {
      pending.push(...current.children);
    }
  }
  return meshes;
}


export function clearComponentSelection() {
  for (const entry of selectedMaterialEntries) {
    const highlighted = Array.isArray(entry.highlighted)
      ? entry.highlighted
      : [entry.highlighted];
    entry.mesh.material = entry.original;
    for (const material of highlighted) {
      if (material !== entry.original && material && typeof material.dispose === 'function') {
        material.dispose();
      }
    }
  }
  selectedMaterialEntries = [];
}


export function selectComponent(object) {
  clearComponentSelection();
  const selected = findSelectableAncestor(object);
  if (!selected) return null;

  for (const mesh of meshesWithin(selected)) {
    const original = mesh.material;
    const highlighted = Array.isArray(original)
      ? original.map(cloneHighlightedMaterial)
      : cloneHighlightedMaterial(original);
    mesh.material = highlighted;
    selectedMaterialEntries.push({ mesh, original, highlighted });
  }

  const component = selected.userData.sculptComponent;
  return {
    id: component.id,
    name: component.name,
    kind: component.visualKind || component.role,
    confidence: component.sourceConfidence ?? component.confidence,
  };
}
