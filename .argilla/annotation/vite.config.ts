import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import monacoEditorPlugin from "vite-plugin-monaco-editor";

export default defineConfig({
  plugins: [
    react(),
    (monacoEditorPlugin as unknown as typeof monacoEditorPlugin.default).default({
      languageWorkers: ["typescript", "editorWorkerService"],
    }),
  ],
  test: {
    environment: "jsdom",
    include: ["tests/unit/**/*.spec.ts"],
  },
  build: {
    lib: {
      entry: "src/index.tsx",
      name: "AnnotationView",
      formats: ["iife"],
      fileName: () => "code-annotation.bundle.js",
    },
    rollupOptions: {
      moduleTypes: { ".ttf": "dataurl" },
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
