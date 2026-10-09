'use strict';
module.exports = {
  name: 'thumbnail',
  async prepare(context) {
    const works = await context.works();
    const asset = works.items.find(w => w.thumbnail_asset_id)?.thumbnail_asset_id;
    if (!asset) throw Error('No ready thumbnail in first 20 works');
    return {path: '/media/thumb/' + asset, thumbnail_asset_id: asset};
  },
  async execute(context, prepared, {via}) {
    const response = await context.request(prepared.path, via);
    return {status: response.status, bytes: (await response.arrayBuffer()).byteLength, measurements: response.perfMeasurements};
  },
};
