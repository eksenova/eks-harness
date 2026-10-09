import scene from "./index";

declare global {
  interface Window {
    ehx?: typeof scene;
  }
}

if (typeof window !== "undefined") window.ehx = scene;
