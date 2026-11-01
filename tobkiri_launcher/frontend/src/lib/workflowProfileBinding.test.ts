import assert from 'node:assert/strict';
import test from 'node:test';
import {bindWorkflowProfile} from './workflowProfileBinding';
import type {ApiDynamicFrontendCatalog} from './apiTypes';
const catalog={version:'rumi.ui.contribution.v1',profile_id:'defaults',profile_revision:'revision',activation_id:'activation:defaults-one',plan_hash:'plan',catalog_hash:'catalog',contributions:[],diagnostics:[],quarantined_pack_ids:[]} as ApiDynamicFrontendCatalog;
const binding={profileId:'defaults',profileRevision:'revision',planDigest:'plan'};
test('Flow editor refuses a different Profile before any write',async()=>{
  let invokes=0;
  const client=bindWorkflowProfile(binding,{fetchCatalog:async()=>({...catalog,profile_id:'other'}),invoke:async()=>{invokes++;return {};}});
  await assert.rejects(()=>client.fetchCatalog(),/Profile/);assert.equal(invokes,0);
});
test('Flow editor captures one activation and rejects a successor even with identical Profile and plan',async()=>{
  let current=catalog;
  const client=bindWorkflowProfile(binding,{fetchCatalog:async()=>current,invoke:async()=>({})});
  assert.equal(await client.fetchCatalog(),catalog);
  current={...catalog,activation_id:'activation:defaults-two'};
  await assert.rejects(()=>client.fetchCatalog(),/実行対象/);
});
test('Flow editor never discovers a writable target without the validated runtime surface',async()=>{
  let reads=0;
  const client=bindWorkflowProfile(null,{fetchCatalog:async()=>{reads++;return catalog;},invoke:async()=>({})});
  await assert.rejects(()=>client.fetchCatalog(),/有効なProfile/);assert.equal(reads,0);
});
