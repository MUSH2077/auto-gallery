import type { QueryClient } from "@tanstack/react-query";

const PRIVATE_ROOT = ["remote-discovery-private"] as const;
const controllersByUser = new Map<number, Set<AbortController>>();

export async function runPrivateDiscoveryRequest<T>(
  userId: number,
  request: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  return startPrivateDiscoveryRequest(userId, request).promise;
}

export function startPrivateDiscoveryRequest<T>(
  userId: number,
  request: (signal: AbortSignal) => Promise<T>,
): { promise: Promise<T>; cancel: () => void } {
  const controller = new AbortController();
  const controllers = controllersByUser.get(userId) || new Set<AbortController>();
  controllers.add(controller);
  controllersByUser.set(userId, controllers);
  const promise = (async () => {
    try {
      return await request(controller.signal);
    } finally {
      controllers.delete(controller);
      if (controllers.size === 0) controllersByUser.delete(userId);
    }
  })();
  return { promise, cancel: () => controller.abort() };
}

export function clearPrivateDiscoveryCache(queryClient: QueryClient, userId?: number) {
  const queryKey = userId === undefined ? PRIVATE_ROOT : [...PRIVATE_ROOT, userId] as const;
  const controllerGroups = userId === undefined
    ? [...controllersByUser.entries()]
    : [[userId, controllersByUser.get(userId) || new Set<AbortController>()] as const];
  for (const [ownerId, controllers] of controllerGroups) {
    for (const controller of controllers) controller.abort();
    controllersByUser.delete(ownerId);
  }
  void queryClient.cancelQueries({ queryKey });
  queryClient.removeQueries({ queryKey });
  for (const mutation of queryClient.getMutationCache().findAll({ mutationKey: queryKey })) {
    queryClient.getMutationCache().remove(mutation);
  }
}
