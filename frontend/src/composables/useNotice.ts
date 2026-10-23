import { useToast } from '@nuxt/ui/composables/useToast'

export function useNotice() {
  const toast = useToast()
  return {
    success: (title: string) => toast.add({ title, color: 'success' }),
    warning: (title: string) => toast.add({ title, color: 'warning' }),
    error: (title: string) => toast.add({ title, color: 'error' }),
  }
}
