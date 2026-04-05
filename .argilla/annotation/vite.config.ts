import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import monacoEditorPlugin from "vite-plugin-monaco-editor";

export default defineConfig({
  plugins: [
    react(),
    (monacoEditorPlugin as unknown as typeof monacoEditorPlugin.default).default({
      languageWorkers: ["typescript", "editorWorkerService"],
      customWorkers: [
        { label: "python", entry: "monaco-editor/esm/vs/basic-languages/python/python.js" },
      ],
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
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});
