// Відкрити інструкцію з будь-якого місця застосунку: App слухає цю подію
// і показує GuideScreen поверх поточного екрана (section — id розділу).
export const GUIDE_EVENT = "ruflo:open-guide";

export function openGuide(section = null) {
  window.dispatchEvent(new CustomEvent(GUIDE_EVENT, { detail: { section } }));
}
