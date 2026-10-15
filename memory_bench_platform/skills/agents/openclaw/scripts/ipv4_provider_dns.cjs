// Opt-in workaround for provider hosts that publish AAAA records on IPv4-only Docker networks.
const dns = require('node:dns');
const dnsPromises = require('node:dns/promises');
const { syncBuiltinESMExports } = require('node:module');

const hosts = new Set((process.env.MEMORY_BENCH_IPV4_ONLY_HOSTS || '')
  .split(',').map((host) => host.trim().toLowerCase()).filter(Boolean));

if (hosts.size) {
  const lookup = dns.lookup;
  const promiseLookup = dnsPromises.lookup;
  const optionsFor = (host, options) => hosts.has(String(host).toLowerCase())
    ? { ...(typeof options === 'object' && options !== null ? options : {}), family: 4 }
    : options;
  dns.lookup = (host, options, callback) => {
    if (typeof options === 'function') return lookup(host, optionsFor(host, undefined), options);
    return lookup(host, optionsFor(host, options), callback);
  };
  dnsPromises.lookup = (host, options) => promiseLookup(host, optionsFor(host, options));
  syncBuiltinESMExports();
}
