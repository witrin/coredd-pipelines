import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { MonacoStrip } from "./.vite/plugin.js";

export default defineConfig(({ command }) => ({
    plugins: [new MonacoStrip(), react()],
    optimizeDeps: {
        include: ["monaco-editor"],
    },
    test: {
        environment: "jsdom",
        include: ["tests/unit/**/*.spec.ts"],
    },
    define: {
        "process.env.NODE_ENV": JSON.stringify("production"),
    },
    build: {
        lib: {
            entry: "src/index.tsx",
            name: "AnnotationView",
            formats: ["iife"],
            fileName: () => "bundle.js",
        },
        rollupOptions: {
            moduleTypes: { ".ttf": "dataurl" },
            output: {
                inlineDynamicImports: true,
            },
        },
    },
    base: command === "build" ? "/annotation/" : "/",
}));
