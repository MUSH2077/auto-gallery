'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function ImportJobsRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace('/admin/jobs?tab=imports');
  }, [router]);
  return null;
}
