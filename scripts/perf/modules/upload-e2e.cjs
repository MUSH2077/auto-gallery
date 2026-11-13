"use strict";
const base=require('./upload.cjs');const browseCheck=require('./browse-check.cjs');
module.exports={...base,name:'upload-e2e',async execute(context,prepared,options){
 const result=await base.execute(context,prepared,options);
 if(result.status===200)result.measurements.browse=await browseCheck(context,result.measurements.snapshots.assets);
 return result;
}};
