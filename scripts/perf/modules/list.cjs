'use strict';
module.exports = {
  name: 'list',
  async prepare(context) {
    const works = await context.works();
    return {path: '/api/v1/works?offset=0&limit=20', visible_works: works.total};
  },
  async execute(context, prepared, {via}) {
    const response = await context.request(prepared.path, via);
    return {status: response.status, bytes: (await response.arrayBuffer()).byteLength, measurements: response.perfMeasurements};
  },
};
