import assert from "node:assert/strict";
import test from "node:test";
import { canonicalRequestQuery } from "../../e2e/contractRequestMatcher";
import { DEFAULTSPACK_CONTRACT_ENDPOINT, defaultspackContractRoute, defaultspackContractUrl } from "./api";

const path = "api/chat/conversation";
const request = (url: string, method = "GET") => ({url: () => url, method: () => method});
const target = (query: string, method = "GET") => defaultspackContractUrl(defaultspackContractRoute(path + query), method);

test("fixture matches canonical method/path and decodes embedded query once", () => {
  for (const id of ["conversation-a", "conversation-b", "space & ? # / 日本語"]){
    const query = new URLSearchParams({conversation_id:id,limit:"7"}).toString();
    const result = canonicalRequestQuery(request(target("?" + query)),path,"GET");
    assert.deepEqual(result?.getAll("conversation_id"),[id]);
    assert.equal(result?.get("limit"),"7");
  }
  assert.equal(canonicalRequestQuery(request(target("?conversation_id=a")),path,"GET")?.get("conversation_id"),"a");
  assert.notEqual(canonicalRequestQuery(request(target("?conversation_id=b")),path,"GET")?.get("conversation_id"),"a");
});

test("fixture refuses legacy fallback, mismatched methods, malformed encoding and another route", () => {
  assert.equal(canonicalRequestQuery(request(defaultspackContractRoute(path).apiPath),path,"GET"),null);
  assert.equal(canonicalRequestQuery(request(target("","PUT")),path,"GET"),null);
  assert.equal(canonicalRequestQuery(request(target(""),"PUT"),path,"GET"),null);
  assert.equal(canonicalRequestQuery(request(DEFAULTSPACK_CONTRACT_ENDPOINT + "%ZZ"),path,"GET"),null);
  assert.equal(canonicalRequestQuery(request(target("")),"api/chat/turns","GET"),null);
  assert.equal(canonicalRequestQuery(request(target("?conversation_id=a&conversation_id=b")),path,"GET")?.getAll("conversation_id").length,2);
});
