// This module is only imported by the lazy editor chunk. Python tokenization and
// the worker are bundled on the application origin; source never goes to a CDN.
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/languages/definitions/python/register.js";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";

(
  self as typeof self & { MonacoEnvironment?: monaco.Environment }
).MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};
loader.config({ monaco });
