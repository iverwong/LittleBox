import { create } from 'zustand'

export type ToastVariant = 'info' | 'success' | 'warning' | 'error'

export interface ToastItem {
	id: number
	message: string
	variant: ToastVariant
	duration: number
	timerId?: ReturnType<typeof setTimeout>
}

export interface ToastOptions {
	message: string
	variant?: ToastVariant
	duration?: number
}

interface ToastStore {
	toasts: ToastItem[]
	addToast: (opts: ToastOptions) => void
	removeToast: (id: number) => void
	clearTimer: (id: number) => void
}

let nextId = 1

export const useToastStore = create<ToastStore>((set, get) => ({
	toasts: [],
	addToast: ({ message, variant = 'info', duration = 3000 }) => {
		const id = nextId++
		const timerId = setTimeout(() => {
			get().removeToast(id)
		}, duration)
		set((state) => ({
			toasts: [...state.toasts, { id, message, variant, duration, timerId }],
		}))
	},
	removeToast: (id) => {
		// Clear any pending timer before removing
		const toast = get().toasts.find((t) => t.id === id)
		if (toast?.timerId) {
			clearTimeout(toast.timerId)
		}
		set((state) => ({
			toasts: state.toasts.filter((t) => t.id !== id),
		}))
	},
	clearTimer: (id) => {
		const toast = get().toasts.find((t) => t.id === id)
		if (toast?.timerId) {
			clearTimeout(toast.timerId)
		}
	},
}))

export const toast = {
	show: ({ message, variant = 'info', duration = 3000 }: ToastOptions) => {
		useToastStore.getState().addToast({ message, variant, duration })
	},
}
