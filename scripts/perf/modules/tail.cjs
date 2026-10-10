'use strict';
const {python}=require('../bridge.cjs');
module.exports={
  name:'tail',mode:'finite',via:['native'],concurrency:[1],
  async prepare(context){return {tasks:1,assets:context.config.fixtures.assets.length};},
  async execute(context,prepared,options){
    const kind=context.config.tail_kind||'mixed';
    const label=context.config.run_id+'-'+kind+'-'+options.repeat;
    const measurements=await python(context,'control.py',{action:'tail',
      payload:{action:'case',kind,label,timeout:context.config.timeout_seconds}});
    return {status:200,bytes:0,measurements};
  },
};
