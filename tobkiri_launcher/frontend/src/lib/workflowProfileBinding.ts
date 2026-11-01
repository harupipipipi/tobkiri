import type {ApiDynamicFrontendCatalog} from './apiTypes';
import {fetchFrontendCatalog,invokeFrontendCapability} from './defaultspackClient';
import type {WorkflowAuthoringDependencies} from './workflowAuthoring';

export interface WorkflowProfileBinding {profileId:string;profileRevision:string;planDigest:string}
const identity=(catalog:ApiDynamicFrontendCatalog)=>JSON.stringify([catalog.profile_id,catalog.profile_revision,catalog.activation_id,catalog.plan_hash,catalog.catalog_hash]);
/** One editor instance never silently retargets its draft or Run to a successor. */
export function bindWorkflowProfile(expected:WorkflowProfileBinding|null,dependencies?:WorkflowAuthoringDependencies):WorkflowAuthoringDependencies {
  const original=dependencies??{fetchCatalog:fetchFrontendCatalog,invoke:invokeFrontendCapability};
  let captured:string|null=null;
  return {...original,fetchCatalog:async()=>{
    if(!expected)throw new Error('有効なProfileを確認してからFlowを開いてください');
    const catalog=await original.fetchCatalog();
    if(catalog.profile_id!==expected.profileId||catalog.profile_revision!==expected.profileRevision||catalog.plan_hash!==expected.planDigest)throw new Error('Profileが切り替わりました。Flowを開き直して対象を確認してください');
    const current=identity(catalog);
    if(captured!==null&&captured!==current)throw new Error('Flowの実行対象が更新されました。変更を確認してから開き直してください');
    captured=current;
    return catalog;
  }};
}
