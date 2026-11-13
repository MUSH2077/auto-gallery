'use strict';
module.exports = {
  name: 'search',
  async prepare(context) {
    const works = await context.works();
    const titles = [...works.items];
    if (!context.config.search) {
      // Recently imported Danbooru works often have no title; look further back.
      for (let offset = 20; offset < Math.min(works.total, 1000); offset += 100) {
        const page = await context.json(`/api/v1/works?offset=${offset}&limit=100`);
        titles.push(...page.items);
        if (page.items.some(w => w.title)) break;
      }
    }
    const candidates = context.config.search ? [context.config.search] :
      [...new Set(titles.flatMap(w => (w.title || '').match(/[\p{L}\p{N}]{2,}/gu) || []))].slice(0, 10);
    for (const search of candidates) {
      const path = '/api/v1/works?q=' + encodeURIComponent(search) + '&limit=20';
      const found = await context.json(path);
      if (found.items?.length) return {path, search, search_hits: found.total};
    }
    throw Error('No nonempty text search found; supply --search with a known indexed term');
  },
  async execute(context, prepared, {via}) {
    const response = await context.request(prepared.path, via);
    return {status: response.status, bytes: (await response.arrayBuffer()).byteLength, measurements: response.perfMeasurements};
  },
};
