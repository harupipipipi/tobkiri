import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";
import * as esbuild from "esbuild";
import { createExtension } from "../pi-shim/index.js";
import { toCanonFloat } from "../pack-ui/float.js";

const root = path.resolve(import.meta.dirname, "../../..");
const loadOriginal = async (name) => {
  const directory = path.join(root, "packs", `local.pi-example-${name}`);
  const pin = JSON.parse(await readFile(path.join(directory, "upstream.json")));
  const original = await readFile(path.join(directory, pin.stored_path));
  assert.equal(createHash("sha256").update(original).digest("hex"), pin.sha256);
  const bundle = await esbuild.build({ entryPoints: [path.join(directory, pin.stored_path)],
    bundle: true, format: "esm", platform: "neutral", write: false,
    alias: { "@earendil-works/pi-coding-agent": path.join(root, "sdk/js/pi-shim/index.js"),
             "@earendil-works/pi-ai": path.join(root, "sdk/js/pi-shim/pi-ai.js") } });
  return (await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`)).default;
};

test("pinned unchanged hello registers a real TypeBox tool and invokes it", async () => {
  const extension = createExtension(await loadOriginal("hello"), { notify() {}, hasUI: () => false, isCancelled: () => false });
  assert.equal(extension.load().tools[0].name, "hello");
  assert.equal(JSON.parse(extension.load().tools[0].parameters).properties.name.type, "string");
  const result = JSON.parse(await extension.invokeTool("hello", "call-1", '{"name":"Tobkiri"}'));
  assert.deepEqual(result, { content: [{ type: "text", text: "Hello, Tobkiri!" }], details: { greeted: "Tobkiri" } });
});

test("pinned protected-paths returns a block before dispatch and allows other paths", async () => {
  const notifications = [];
  const extension = createExtension(await loadOriginal("protected-paths"), { notify: (text, level) => notifications.push({ text, level }), hasUI: () => true, isCancelled: () => false });
  const blocked = JSON.parse(await extension.onEvent("tool_call", '{"toolName":"write","input":{"path":"project/.env"}}', true));
  assert.equal(blocked.block, true);
  assert.equal(notifications.length, 1);
  assert.equal(await extension.onEvent("tool_call", '{"toolName":"write","input":{"path":"project/readme.md"}}', true), undefined);
});

test("unsupported pi APIs fail explicitly", () => {
  assert.throws(() => createExtension((pi) => pi.sendMessage({}), {}), /unsupported by tobkiri.pi-adapter: pi.sendMessage/);
  assert.throws(() => createExtension((pi) => pi.on("session_start", () => {}), {}), /unsupported by tobkiri.pi-adapter: on\(session_start\)/);
});

test("WIT float strings preserve width, extremes and negative zero", () => {
  for (const [number, width, expected] of [[1.5,"f64","1.5e0"],[0.1,"f32","1e-1"],[0.1,"f64","1e-1"],[-0,"f64","-0e0"],[-0,"f32","-0e0"],[1e300,"f64","1e300"],[Number.MIN_VALUE,"f64","5e-324"],[2**-149,"f32","1e-45"],[Math.fround(3.4028234663852886e38),"f32","3.4028235e38"]]) {
    assert.equal(toCanonFloat(number, width), expected);
  }
  for (const value of [NaN, Infinity, -Infinity]) assert.throws(() => toCanonFloat(value, "f64"), /non-finite/);
  assert.throws(() => toCanonFloat(1e100, "f32"), /overflow/);
});

test("JS float conversion equals Rust declared-width golden cases", async () => {
  const vectors=JSON.parse(await readFile(new URL("./float-golden.json",import.meta.url),"utf8"));
  for (const vector of vectors) {
    const buffer=new ArrayBuffer(8);const view=new DataView(buffer);
    let number;
    if(vector.width==="f64"){view.setBigUint64(0,BigInt(`0x${vector.bits}`));number=view.getFloat64(0);}
    else{view.setUint32(0,Number.parseInt(vector.bits,16));number=view.getFloat32(0);}
    assert.equal(toCanonFloat(number,vector.width),vector.expect,`${vector.width} bits ${vector.bits}`);
  }
});
