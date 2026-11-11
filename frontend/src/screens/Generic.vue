<script setup>
// A stage without a screen of its own: the common frame (with View JSON) is all it shows.
import StageFrame from '../StageFrame.vue'

defineProps({ stage: { type: String, required: true } })
</script>

<template>
  <StageFrame :stage="stage" />
</template>
